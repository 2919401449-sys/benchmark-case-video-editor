#!/usr/bin/env python3
"""Convert edit_plan.json into a non-destructive Jianying draft.

This adapter deliberately keeps edit_plan.json as the source of truth.  It can
write a plaintext package for structural validation, or use Jianying's local
videoeditor.dll codec when a tested Jianying installation is supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image


ADAPTER_VERSION = "0.2.0"
TESTED_JIANYING_VERSION = "11.4.2 / 11.5.0 structural"


def caption_plan_signature(plan):
    data={'timeline':plan['timeline'],'a_roll':plan['tracks']['a_roll'],
          'subtitles':plan['tracks'].get('subtitles',[])}
    return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def graphic_z_group(clip):
    if clip.get('template') == 'series_branding':
        return 100
    if clip.get('template') == 'background_blur':
        return 0
    return int(clip.get('z_group', 10 if clip.get('layer_key') == 'base' else 30))


def allocate_graphic_lanes(clips):
    """Separate z groups before interval packing: blur must never cover logos."""
    lanes, ends, groups = [], [], []
    for clip in sorted(clips, key=lambda c: (graphic_z_group(c), float(c['timeline_in']), float(c['timeline_out']))):
        group = graphic_z_group(clip)
        start = float(clip['timeline_in'])
        index = next((i for i, end in enumerate(ends) if groups[i] == group and start >= end-0.001), None)
        if index is None:
            index = len(lanes)
            lanes.append([])
            ends.append(-1)
            groups.append(group)
        lanes[index].append(clip)
        ends[index] = float(clip['timeline_out'])
    return lanes


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class MediaRecord:
    path: str
    duration_seconds: float
    width: int
    height: int
    material_type: str = "video"


def build_media_map(inventory: dict[str, Any]) -> dict[str, MediaRecord]:
    result: dict[str, MediaRecord] = {}
    for group in ("a_roll", "b_roll", "graphics"):
        for item in inventory.get(group, []):
            path = str(Path(item["path"]).resolve())
            result[os.path.normcase(path)] = MediaRecord(
                path=path,
                duration_seconds=float(item["duration_seconds"]),
                width=int(item["width"]),
                height=int(item["height"]),
            )
    return result


def install_dependency_shims(media_map: dict[str, MediaRecord]) -> None:
    """Supply the two optional runtime dependencies we do not use directly.

    pyJianYingDraft normally asks MediaInfo for dimensions and duration, but the
    pipeline has already inspected every source with FFmpeg.  Reusing that
    inventory makes the adapter deterministic and avoids another native install.
    The UI automation module is stubbed because this adapter never controls the
    Jianying UI or automatic export.
    """

    class Track(types.SimpleNamespace):
        pass

    class ParsedInfo:
        def __init__(self, record: MediaRecord):
            duration_ms = record.duration_seconds * 1000.0
            self.video_tracks = [] if record.material_type == "photo" else [
                Track(duration=duration_ms, width=record.width, height=record.height)
            ]
            self.general_tracks = [Track(duration=duration_ms)]
            self.audio_tracks: list[Any] = []
            self.image_tracks = (
                [Track(width=record.width, height=record.height)]
                if record.material_type == "photo"
                else []
            )

    class MediaInfo:
        @staticmethod
        def can_parse() -> bool:
            return True

        @staticmethod
        def parse(path: str, **_: Any) -> ParsedInfo:
            key = os.path.normcase(str(Path(path).resolve()))
            if key not in media_map:
                raise FileNotFoundError(f"素材不在 media_inventory.json 中：{path}")
            return ParsedInfo(media_map[key])

    pymediainfo = types.ModuleType("pymediainfo")
    pymediainfo.MediaInfo = MediaInfo  # type: ignore[attr-defined]
    sys.modules.setdefault("pymediainfo", pymediainfo)

    uiautomation = types.ModuleType("uiautomation")
    uiautomation.Control = object  # type: ignore[attr-defined]
    uiautomation.WindowControl = object  # type: ignore[attr-defined]
    sys.modules.setdefault("uiautomation", uiautomation)


def import_draft_library(repo: Path, media_map: dict[str, MediaRecord]):
    if not (repo / "pyJianYingDraft" / "__init__.py").is_file():
        raise FileNotFoundError(f"未找到 pyJianYingDraft：{repo}")
    install_dependency_shims(media_map)
    sys.path.insert(0, str(repo))
    import pyJianYingDraft as draft  # type: ignore

    return draft


def seconds_to_us(value: float) -> int:
    return int(round(float(value) * 1_000_000))


def srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def clean_caption_text(text: str) -> str:
    # Captions intentionally contain no punctuation.  Punctuation that marks a
    # spoken pause becomes a normal space so the reading rhythm remains visible.
    text = re.sub(r"[，,。.!！?？；;：:、—–…]+", " ", text)
    text = re.sub(r"[“”‘’\"'（）()《》〈〉【】\[\]{}·•]+", " ", text)
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff\s]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def split_caption_text(text: str, max_chars: int = 22) -> list[str]:
    text = clean_caption_text(text)
    if not text:
        return []
    clauses = [part for part in text.split(" ") if part]
    lines: list[str] = []
    current = ""
    for clause in clauses:
        if len(clause) > max_chars:
            if current:
                lines.append(current)
                current = ''
            # Balanced splitting avoids isolated one/two-character captions.
            count = math.ceil(len(clause)/max_chars)
            width = math.ceil(len(clause)/count)
            lines.extend(clause[i:i+width] for i in range(0,len(clause),width))
            continue
        if current and len(current.replace(" ", "")) + len(clause) > max_chars:
            lines.append(current)
            current = ""
        current += (" " if current else "") + clause
    if current:
        lines.append(current)
    return lines


def make_srt(subtitles: list[dict[str, Any]], target: Path) -> int:
    entries: list[tuple[float, float, str]] = []
    for subtitle in subtitles:
        start = float(subtitle["timeline_in"])
        end = float(subtitle["timeline_out"])
        parts = split_caption_text(str(subtitle["text"]))
        if not parts or end <= start:
            continue
        weights = [max(1, len(part.replace(" ", ""))) for part in parts]
        total_weight = sum(weights)
        cursor = start
        for index, (part, weight) in enumerate(zip(parts, weights)):
            part_end = end if index == len(parts) - 1 else cursor + (end - start) * weight / total_weight
            entries.append((cursor, part_end, part))
            cursor = part_end

    blocks = []
    for index, (start, end, text) in enumerate(entries, 1):
        blocks.append(
            f"{index}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{text}\n"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(blocks), encoding="utf-8-sig")
    return len(entries)


def validate_plan(plan: dict[str, Any], media_map: dict[str, MediaRecord]) -> None:
    tracks = plan.get("tracks", {})
    if 'caption_segments' in plan:
        if plan.get('quality_checks',{}).get('caption_source_signature') != caption_plan_signature(plan):
            raise ValueError('口播或字幕已变化 请重新执行 editorial_quality.py audit')
        previous_end=0.0
        for caption in plan['caption_segments']:
            if not (0 <= caption['timeline_in'] < caption['timeline_out'] <= plan['timeline']['duration_seconds']):
                raise ValueError('字幕时间码非法')
            if caption['timeline_in'] < previous_end-0.001:
                raise ValueError('语音锚点字幕重叠')
            previous_end=caption['timeline_out']
    if not tracks.get("a_roll"):
        raise ValueError("edit_plan.json 中没有 A-roll 片段")
    for track_name in ("a_roll", "b_roll"):
        previous_end = -1.0
        for clip in tracks.get(track_name, []):
            source = os.path.normcase(str(Path(clip["source"]).resolve()))
            if source not in media_map:
                raise FileNotFoundError(f"{track_name} 素材未登记：{clip['source']}")
            if not Path(clip["source"]).is_file():
                raise FileNotFoundError(f"素材文件不存在：{clip['source']}")
            source_in = float(clip["source_in"])
            source_out = float(clip["source_out"])
            timeline_in = float(clip["timeline_in"])
            timeline_out = float(clip["timeline_out"])
            if source_in < 0 or source_out <= source_in:
                raise ValueError(f"非法源时间范围：{clip}")
            if source_out > media_map[source].duration_seconds + 0.05:
                raise ValueError(f"源出点超过素材时长：{clip['source']}")
            if timeline_out <= timeline_in:
                raise ValueError(f"非法时间线范围：{clip}")
            if track_name == "a_roll" and timeline_in < previous_end - 0.002:
                raise ValueError("A-roll 主轨片段发生重叠或倒序")
            previous_end = max(previous_end, timeline_out)
    for clip in tracks.get("graphics", []):
        source = Path(clip["source"]).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"图表素材不存在：{source}")
        timeline_in = float(clip["timeline_in"])
        timeline_out = float(clip["timeline_out"])
        if not math.isfinite(timeline_in) or not math.isfinite(timeline_out) or timeline_in < 0 or timeline_out <= timeline_in:
            raise ValueError(f"非法图表时间范围：{clip}")
        if timeline_out > float(plan["timeline"]["duration_seconds"]) + 0.05:
            raise ValueError("图表片段超出成片时间线。")
        if source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            record = media_map.get(os.path.normcase(str(source)))
            if record is None:
                raise ValueError(f"动态图表视频未登记在素材清单中：{source}")
            source_in = float(clip.get("source_in", 0))
            source_out = float(clip.get("source_out", source_in + timeline_out - timeline_in))
            if not math.isfinite(source_in) or not math.isfinite(source_out) or source_in < 0 or source_out > record.duration_seconds + 0.05 or abs(source_out - source_in - timeline_out + timeline_in) > 0.05:
                raise ValueError(f"动态图表源片段长度与时间线不一致：{source}")
        supported_properties = {"alpha", "position_x", "position_y", "uniform_scale", "rotation"}
        for keyframe in clip.get("keyframes", []):
            if keyframe.get("property") not in supported_properties:
                raise ValueError(f"不支持的图表关键帧属性：{keyframe}")
            offset = float(keyframe["time"])
            value = float(keyframe["value"])
            if not math.isfinite(offset) or not 0 <= offset <= timeline_out - timeline_in + 0.001:
                raise ValueError(f"图表关键帧超出片段：{keyframe}")
            if not math.isfinite(value):
                raise ValueError(f"图表关键帧数值非法：{keyframe}")
            if keyframe["property"] == "alpha" and not 0 <= value <= 1:
                raise ValueError(f"图表透明度必须在 0 到 1：{keyframe}")


def create_draft(
    *,
    plan: dict[str, Any],
    inventory: dict[str, Any],
    draft_root: Path,
    artifacts_dir: Path,
    draft_name: str,
    repo: Path,
    mode: str,
    jianying_install: Path | None,
    user_data_root: Path | None,
) -> dict[str, Any]:
    media_map = build_media_map(inventory)
    validate_plan(plan, media_map)
    image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    for clip in plan.get("tracks", {}).get("graphics", []):
        source = Path(clip["source"]).resolve()
        key = os.path.normcase(str(source))
        if source.suffix.lower() in image_suffixes and key not in media_map:
            with Image.open(source) as graphic_image:
                width, height = graphic_image.size
            media_map[key] = MediaRecord(
                path=str(source),
                duration_seconds=10800.0,
                width=int(width),
                height=int(height),
                material_type="photo",
            )
    draft = import_draft_library(repo, media_map)

    draft_root.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    if (draft_root / draft_name).exists():
        raise FileExistsError(f"为避免覆盖，已停止：草稿已存在 {draft_root / draft_name}")

    codec = None
    if mode == "encrypted":
        if jianying_install is None:
            raise ValueError("encrypted 模式必须提供 --jianying-install")
        codec = draft.JianyingDraftCryptoCodec(
            draft.DraftCryptoConfig(jy_install_dir=str(jianying_install), backup=True)
        )

    folder = draft.DraftFolder(
        str(draft_root),
        content_codec=codec,
        user_data_path=str(user_data_root) if user_data_root else str(artifacts_dir / "User Data"),
    )
    timeline = plan["timeline"]
    script = folder.create_draft(
        draft_name,
        int(timeline["width"]),
        int(timeline["height"]),
        fps=int(round(float(timeline["fps"]))),
        allow_replace=False,
    )
    # DraftFolder intentionally creates new drafts as plaintext.  For modern
    # Jianying, opt in explicitly so ScriptFile.save() uses the tested codec.
    if codec is not None:
        script._loaded_content_codec = codec
    b_roll_clips = sorted(
        plan["tracks"].get("b_roll", []),
        key=lambda item: (float(item["timeline_in"]), float(item["timeline_out"])),
    )
    b_roll_lanes: list[list[dict[str, Any]]] = []
    b_roll_lane_ends: list[float] = []
    for clip in b_roll_clips:
        start = float(clip["timeline_in"])
        lane_index = next(
            (index for index, end in enumerate(b_roll_lane_ends) if start >= end - 0.001),
            None,
        )
        if lane_index is None:
            lane_index = len(b_roll_lanes)
            b_roll_lanes.append([])
            b_roll_lane_ends.append(-1.0)
        b_roll_lanes[lane_index].append(clip)
        b_roll_lane_ends[lane_index] = float(clip["timeline_out"])

    graphic_lanes = allocate_graphic_lanes(plan['tracks'].get('graphics', []))

    track_specs = [draft.TrackSpec(draft.TrackType.video, "A-roll 主轨")]
    track_specs.extend(
        draft.TrackSpec(draft.TrackType.video, f"B-roll 覆盖 {index + 1}", mute=True)
        for index in range(len(b_roll_lanes))
    )
    track_specs.extend(
        draft.TrackSpec(draft.TrackType.video,
                        ('图表下方 模糊背景' if graphic_z_group(lane[0]) == 0 else
                         f'系列Logo {index+1}' if graphic_z_group(lane[0]) == 100 else f'信息图表 {index+1}'), mute=True)
        for index, lane in enumerate(graphic_lanes)
    )
    track_refs = script.append_tracks(track_specs)
    a_track = track_refs[0]
    b_track_refs = track_refs[1 : 1 + len(b_roll_lanes)]
    graphic_track_refs = track_refs[1 + len(b_roll_lanes) :]

    for clip in plan["tracks"]["a_roll"]:
        source_duration = float(clip["source_out"]) - float(clip["source_in"])
        segment = draft.VideoSegment(
            str(Path(clip["source"]).resolve()),
            draft.trange(seconds_to_us(clip["timeline_in"]), seconds_to_us(source_duration)),
            source_timerange=draft.trange(
                seconds_to_us(clip["source_in"]), seconds_to_us(source_duration)
            ),
            volume=1.0,
        )
        script.add_segment(segment, track=a_track)

    for lane, track_ref in zip(b_roll_lanes, b_track_refs):
        for clip in lane:
            source_duration = float(clip["source_out"]) - float(clip["source_in"])
            target_duration = float(clip["timeline_out"]) - float(clip["timeline_in"])
            duration = min(source_duration, target_duration)
            segment = draft.VideoSegment(
                str(Path(clip["source"]).resolve()),
                draft.trange(seconds_to_us(clip["timeline_in"]), seconds_to_us(duration)),
                source_timerange=draft.trange(
                    seconds_to_us(clip["source_in"]), seconds_to_us(duration)
                ),
                volume=0.0,
            )
            script.add_segment(segment, track=track_ref)

    for lane, track_ref in zip(graphic_lanes, graphic_track_refs):
        for clip in lane:
            source = Path(clip["source"]).resolve()
            target_duration = float(clip["timeline_out"]) - float(clip["timeline_in"])
            target_range = draft.trange(
                seconds_to_us(clip["timeline_in"]), seconds_to_us(target_duration)
            )
            if source.suffix.lower() in image_suffixes:
                segment = draft.VideoSegment(str(source), target_range, volume=0.0)
            else:
                source_in = float(clip.get("source_in", 0.0))
                source_out = float(clip.get("source_out", source_in + target_duration))
                source_duration = min(source_out - source_in, target_duration)
                segment = draft.VideoSegment(
                    str(source),
                    draft.trange(seconds_to_us(clip["timeline_in"]), seconds_to_us(source_duration)),
                    source_timerange=draft.trange(
                        seconds_to_us(source_in), seconds_to_us(source_duration)
                    ),
                    volume=0.0,
                )
            for keyframe in clip.get("keyframes", []):
                segment.add_keyframe(
                    getattr(draft.KeyframeProperty, keyframe["property"]),
                    seconds_to_us(float(keyframe["time"])),
                    float(keyframe["value"]),
                )
            script.add_segment(segment, track=track_ref)

    srt_path = artifacts_dir / f"{draft_name}.srt"
    subtitle_count = make_srt(plan.get('caption_segments', plan["tracks"].get("subtitles", [])), srt_path)
    if subtitle_count:
        # Previous SRT import default size was 5.0. Apply 20% reduction once.
        reference = draft.TextSegment('字幕', draft.trange(0, 1_000_000),
            style=draft.TextStyle(size=4.0, align=1, auto_wrapping=True),
            border=draft.TextBorder(color=(0.0,0.0,0.0), width=20.0))
        script.import_srt(str(srt_path), "中文字幕", style_reference=reference,
                          clip_settings=draft.ClipSettings(transform_y=-0.88))

    script.save()
    draft_path = draft_root / draft_name
    content_path = draft_path / "draft_content.json"
    if not content_path.is_file() or content_path.stat().st_size == 0:
        raise RuntimeError("草稿内容文件未生成")

    decoded = None
    if codec is None:
        decoded = load_json(content_path)
    else:
        decoded = codec.decode(content_path.read_bytes())
    exported_tracks = decoded.get("tracks", [])
    exported_segments = sum(len(track.get("segments", [])) for track in exported_tracks)
    expected_segments = (
        len(plan["tracks"]["a_roll"])
        + len(plan["tracks"].get("b_roll", []))
        + len(plan["tracks"].get("graphics", []))
        + subtitle_count
    )
    if exported_segments != expected_segments:
        raise RuntimeError(
            f"草稿结构校验失败：应有 {expected_segments} 个片段，实际 {exported_segments} 个"
        )

    report = {
        "schema_version": 1,
        "adapter_version": ADAPTER_VERSION,
        "target_jianying_version": TESTED_JIANYING_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "draft_name": draft_name,
        "draft_path": str(draft_path.resolve()),
        "mode": mode,
        "source_edit_plan": str(Path(plan.get("_source_path", "")).resolve()),
        "timeline": timeline,
        "counts": {
            "a_roll": len(plan["tracks"]["a_roll"]),
            "b_roll": len(plan["tracks"].get("b_roll", [])),
            "subtitles": subtitle_count,
            "graphics": len(plan["tracks"].get("graphics", [])),
            "b_roll_lanes": len(b_roll_lanes),
            "graphic_lanes": len(graphic_lanes),
            "tracks": len(exported_tracks),
            "segments": exported_segments,
        },
        "checks": {
            "source_files_exist": True,
            "time_ranges_valid": True,
            "draft_roundtrip_valid": True,
            "jianying_ui_open_verified": False,
        },
        "warnings": [
            "剪映草稿格式不是公开标准，必须在当前安装版本中打开后再确认。",
            "自动导出成片未启用；请在剪映中人工检查并导出。",
            "edit_plan.json 仍是本项目的事实来源。",
        ],
    }
    report_path = artifacts_dir / "jianying_export_report.json"
    write_json(report_path, report)
    return report


def parse_args() -> argparse.Namespace:
    # Ship the tested adapter with the skill so a cloned repository is portable.
    # --repo remains available for maintainers who deliberately test another
    # pyJianYingDraft revision.
    default_repo = Path(__file__).resolve().parents[1] / "vendor" / "pyJianYingDraft-aoguai"
    parser = argparse.ArgumentParser(description="把 edit_plan.json 转成剪映可编辑草稿")
    parser.add_argument("--edit-plan", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--draft-root", type=Path, required=True)
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--draft-name", required=True)
    parser.add_argument("--repo", type=Path, default=default_repo)
    parser.add_argument("--mode", choices=("plain", "encrypted"), default="plain")
    parser.add_argument("--jianying-install", type=Path)
    parser.add_argument("--user-data-root", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan = load_json(args.edit_plan)
    plan["_source_path"] = str(args.edit_plan.resolve())
    report = create_draft(
        plan=plan,
        inventory=load_json(args.inventory),
        draft_root=args.draft_root,
        artifacts_dir=args.artifacts_dir,
        draft_name=args.draft_name,
        repo=args.repo,
        mode=args.mode,
        jianying_install=args.jianying_install,
        user_data_root=args.user_data_root,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
