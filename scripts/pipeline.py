#!/usr/bin/env python3
"""MVP pipeline for script-led customer benchmark videos.

The source project is always read-only. All generated artifacts are written to
the work directory supplied by the caller.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mxf", ".avi", ".mkv", ".mts", ".m2ts"}
SCRIPT_EXTENSIONS = {".txt", ".md", ".docx"}
SKILL_ROOT = Path(__file__).resolve().parent.parent
COMMON_AI_BROLL_ROOT = SKILL_ROOT / "assets" / "common-broll" / "ai-design-platform"

try:
    from opencc import OpenCC

    _OPENCC = OpenCC("t2s")
except ImportError:
    _OPENCC = None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def find_ffmpeg() -> str:
    configured = os.environ.get("VIDEO_EDIT_FFMPEG")
    if configured and Path(configured).is_file():
        return configured
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError(
            "未找到 FFmpeg。请先运行 scripts/setup.ps1，或设置 VIDEO_EDIT_FFMPEG。"
        ) from exc


def run_process(command: list[str], *, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode and not allow_failure:
        raise RuntimeError(f"命令执行失败：{' '.join(command)}\n{result.stderr[-3000:]}")
    return result


def parse_rate(text: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    return float(match.group(1)) if match else None


def probe_media(path: Path, ffmpeg: str) -> dict[str, Any]:
    result = run_process([ffmpeg, "-hide_banner", "-i", str(path)], allow_failure=True)
    text = result.stderr
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    duration = None
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    video_line = next((line for line in text.splitlines() if " Video: " in line), "")
    audio_line = next((line for line in text.splitlines() if " Audio: " in line), "")
    dimensions = re.search(r"(?<!\d)(\d{2,5})x(\d{2,5})(?!\d)", video_line)
    video_codec = re.search(r"Video:\s*([^,\s]+)", video_line)
    audio_codec = re.search(r"Audio:\s*([^,\s]+)", audio_line)

    return {
        "path": str(path.resolve()),
        "name": path.name,
        "size_bytes": path.stat().st_size,
        "duration_seconds": round(duration, 3) if duration is not None else None,
        "width": int(dimensions.group(1)) if dimensions else None,
        "height": int(dimensions.group(2)) if dimensions else None,
        "fps": parse_rate(video_line),
        "video_codec": video_codec.group(1) if video_codec else None,
        "audio_codec": audio_codec.group(1) if audio_codec else None,
        "has_audio": bool(audio_line),
    }


def list_videos(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda p: p.name.lower(),
    )


def find_script(project: Path) -> Path | None:
    candidates = sorted(
        p
        for p in project.iterdir()
        if p.is_file()
        and p.suffix.lower() in SCRIPT_EXTENSIONS
        and not p.name.startswith("~$")
    )
    return candidates[0] if candidates else None


def detect_project_case_type(project: Path, script: Path | None = None) -> str | None:
    """Identify the series without treating V0 as edit copy."""
    folder_hint = project.name.lower()
    if "生产对接" in folder_hint or "设计生产一体化" in folder_hint:
        return "production"
    if "ai智能设计平台" in folder_hint or "ai设计平台" in folder_hint:
        return "ai"
    if script and script.suffix.lower() in {".txt", ".md"}:
        try:
            heading = script.read_text(encoding="utf-8-sig")[:500].lower()
        except (OSError, UnicodeError):
            heading = ""
        if "ai标杆" in heading or "ai智能设计平台" in heading:
            return "ai"
    return None


def load_common_ai_broll() -> list[dict[str, Any]]:
    manifest_path = COMMON_AI_BROLL_ROOT / "manifest.json"
    if not manifest_path.is_file():
        return []
    manifest = load_json(manifest_path)
    entries = []
    for item in manifest.get("clips", []):
        source = (COMMON_AI_BROLL_ROOT / str(item["path"])).resolve()
        if not source.is_file() or source.suffix.lower() not in VIDEO_EXTENSIONS:
            raise FileNotFoundError(f"通用 B-roll 清单中的素材不存在：{source}")
        entries.append(
            {
                "path": source,
                "description": str(item.get("description") or "").strip(),
                "tags": [str(tag) for tag in item.get("tags", [])],
                "origin": "skill_common_ai",
            }
        )
    return entries


def media_source_key(value: str | Path) -> str:
    return str(Path(value).resolve()).lower()


def broll_source_use_limit(item: dict[str, Any], default_limit: int) -> int:
    """Common AI recordings are intentionally one-shot visual accents."""
    return 1 if item.get("origin") == "skill_common_ai" else max(1, default_limit)


def broll_origin_priority_bonus(item: dict[str, Any]) -> float:
    return 30.0 if item.get("origin") == "skill_common_ai" else 0.0


def inspect_project(project: Path, work: Path) -> dict[str, Any]:
    ffmpeg = find_ffmpeg()
    script = find_script(project)
    aroll = list_videos(project / "A-roll")
    project_broll = list_videos(project / "B-roll")
    if not aroll:
        raise FileNotFoundError("没有在 A-roll 文件夹中找到视频。")

    case_type = detect_project_case_type(project, script)
    project_broll_items = []
    for path in project_broll:
        media = probe_media(path, ffmpeg)
        media.update({"origin": "project", "description": "", "tags": []})
        project_broll_items.append(media)
    common_broll_items = []
    if case_type == "ai":
        for common in load_common_ai_broll():
            media = probe_media(common["path"], ffmpeg)
            media.update(
                {
                    "origin": common["origin"],
                    "description": common["description"],
                    "tags": common["tags"],
                }
            )
            common_broll_items.append(media)

    inventory = {
        "schema_version": 1,
        "project_root": str(project.resolve()),
        "case_type": case_type,
        # The transcript-first workflow does not require a V0 script.  Keep a
        # discovered file only as optional project metadata.
        "script": str(script.resolve()) if script else None,
        "a_roll": [probe_media(p, ffmpeg) for p in aroll],
        "b_roll": project_broll_items + common_broll_items,
    }
    inventory["summary"] = {
        "a_roll_files": len(aroll),
        "b_roll_files": len(inventory["b_roll"]),
        "project_b_roll_files": len(project_broll_items),
        "common_b_roll_files": len(common_broll_items),
        "a_roll_seconds": round(sum(x["duration_seconds"] or 0 for x in inventory["a_roll"]), 3),
        "b_roll_seconds": round(sum(x["duration_seconds"] or 0 for x in inventory["b_roll"]), 3),
        "source_bytes": sum(x["size_bytes"] for x in inventory["a_roll"] + inventory["b_roll"]),
    }
    write_json(work / "media_inventory.json", inventory)
    return inventory


def ensure_inventory(project: Path, work: Path) -> dict[str, Any]:
    path = work / "media_inventory.json"
    return load_json(path) if path.exists() else inspect_project(project, work)


def make_thumbnails(project: Path, work: Path, width: int) -> dict[str, Any]:
    inventory = ensure_inventory(project, work)
    ffmpeg = find_ffmpeg()
    thumb_dir = work / "broll_thumbnails"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for media in inventory["b_roll"]:
        source = Path(media["path"])
        duration = float(media.get("duration_seconds") or 0)
        timestamp = max(0.0, min(duration * 0.5, max(duration - 0.1, 0.0)))
        source_hash = hashlib.sha1(media_source_key(source).encode("utf-8")).hexdigest()[:10]
        target = thumb_dir / f"{source.stem}_{source_hash}.jpg"
        if not target.exists():
            run_process(
                [
                    ffmpeg,
                    "-y",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={width}:-2",
                    "-q:v",
                    "3",
                    str(target),
                ]
            )
        entries.append(
            {
                "source": str(source),
                "thumbnail": str(target.resolve()),
                "timestamp_seconds": round(timestamp, 3),
                "description": str(media.get("description") or ""),
                "tags": list(media.get("tags") or []),
                "origin": str(media.get("origin") or "project"),
                "needs_review": False if media.get("origin") == "skill_common_ai" else False,
            }
        )
    result = {"schema_version": 1, "clips": entries}
    write_json(work / "broll_index.json", result)
    return result


def extract_analysis_frames(
    source: Path,
    ffmpeg: str,
    sample_fps: float,
    width: int = 160,
    height: int = 90,
) -> Any:
    """Decode a small grayscale proxy used only for motion/quality analysis."""
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("缺少 numpy。请先运行 scripts/setup.ps1。") from exc

    command = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(source),
        "-an",
        "-vf",
        (
            f"fps={sample_fps},"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,format=gray"
        ),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"分析 B-roll 失败：{source.name}\n{message}")
    frame_size = width * height
    frame_count = len(result.stdout) // frame_size
    if frame_count == 0:
        raise RuntimeError(f"没有从 {source.name} 解码出分析帧")
    return np.frombuffer(result.stdout[: frame_count * frame_size], dtype=np.uint8).reshape(
        frame_count, height, width
    )


def phase_shift(previous: Any, current: Any) -> tuple[float, float, float]:
    """Estimate global translation; stable pans stay smooth while shake produces jerk."""
    import numpy as np

    a = previous.astype(np.float32) - float(previous.mean())
    b = current.astype(np.float32) - float(current.mean())
    spectrum = np.fft.fft2(a) * np.conj(np.fft.fft2(b))
    magnitude = np.abs(spectrum)
    spectrum /= np.maximum(magnitude, 1e-6)
    correlation = np.abs(np.fft.ifft2(spectrum))
    peak_y, peak_x = np.unravel_index(int(np.argmax(correlation)), correlation.shape)
    if peak_x > correlation.shape[1] // 2:
        peak_x -= correlation.shape[1]
    if peak_y > correlation.shape[0] // 2:
        peak_y -= correlation.shape[0]
    confidence = float(correlation.max() / max(float(correlation.mean()), 1e-6))
    if confidence < 4.0:
        return 0.0, 0.0, confidence
    return float(peak_x), float(peak_y), confidence


def normalize_score(value: float, good: float, bad: float) -> float:
    if good == bad:
        return 1.0
    return max(0.0, min(1.0, (bad - value) / (bad - good)))


def analyze_clip_quality(
    media: dict[str, Any],
    ffmpeg: str,
    storyboard_dir: Path,
    sample_fps: float,
    requested_durations: tuple[float, ...],
) -> dict[str, Any]:
    import numpy as np
    from PIL import Image, ImageDraw

    source = Path(media["path"])
    frames = extract_analysis_frames(source, ffmpeg, sample_fps)
    frame_count = len(frames)
    decoded_duration = frame_count / sample_fps
    media_duration = float(media.get("duration_seconds") or decoded_duration)
    duration = min(media_duration, decoded_duration)

    sharpness = []
    exposure = []
    for frame in frames:
        f = frame.astype(np.float32)
        gx = np.diff(f, axis=1)
        gy = np.diff(f, axis=0)
        sharpness.append(float(np.mean(gx * gx) + np.mean(gy * gy)))
        exposure.append(float(np.mean((frame <= 10) | (frame >= 245))))

    shifts: list[tuple[float, float]] = []
    frame_difference = [0.0]
    for previous, current in zip(frames[:-1], frames[1:]):
        shift_x, shift_y, _ = phase_shift(previous, current)
        shifts.append((shift_x, shift_y))
        aligned = np.roll(previous, (-int(round(shift_y)), -int(round(shift_x))), axis=(0, 1))
        frame_difference.append(float(np.mean(np.abs(current.astype(np.float32) - aligned.astype(np.float32)))))

    if shifts:
        velocity = np.asarray(shifts, dtype=np.float32)
        acceleration = np.linalg.norm(np.diff(velocity, axis=0), axis=1)
        motion = np.linalg.norm(velocity, axis=1)
        jitter = np.concatenate(([float(motion[0])], acceleration, [float(acceleration[-1]) if len(acceleration) else float(motion[0])]))
    else:
        jitter = np.zeros(frame_count, dtype=np.float32)

    storyboard_indexes = sorted(
        set(min(frame_count - 1, max(0, int(round((frame_count - 1) * fraction)))) for fraction in (0.12, 0.32, 0.52, 0.72, 0.9))
    )
    panel_width, panel_height = 320, 180
    storyboard = Image.new("RGB", (panel_width * len(storyboard_indexes), panel_height + 28), "#111111")
    draw = ImageDraw.Draw(storyboard)
    for panel_index, frame_index in enumerate(storyboard_indexes):
        panel = Image.fromarray(frames[frame_index], mode="L").convert("RGB").resize((panel_width, panel_height))
        storyboard.paste(panel, (panel_index * panel_width, 0))
        draw.text((panel_index * panel_width + 8, panel_height + 6), f"{frame_index / sample_fps:.1f}s", fill="white")
    storyboard_path = storyboard_dir / f"{source.stem}.jpg"
    storyboard.save(storyboard_path, quality=88)

    best_windows = []
    for requested in requested_durations:
        if duration <= 1.2:
            actual_duration = duration
            edge_guard = 0.0
        else:
            edge_guard = min(0.8, max(0.2, (duration - 1.2) / 2))
            usable_duration = max(1.2, duration - edge_guard * 2)
            actual_duration = min(requested, usable_duration)
        window_frames = min(frame_count, max(1, int(round(actual_duration * sample_fps))))
        step = max(1, int(round(sample_fps * 0.25)))
        starts = list(range(0, max(1, frame_count - window_frames + 1), step))
        final_start = max(0, frame_count - window_frames)
        if final_start not in starts:
            starts.append(final_start)

        candidates = []
        for start_index in starts:
            end_index = min(frame_count, start_index + window_frames)
            start_seconds = start_index / sample_fps
            end_seconds = min(duration, end_index / sample_fps)
            local_jitter = float(np.median(jitter[start_index:end_index]))
            local_sharpness = float(np.median(sharpness[start_index:end_index]))
            local_exposure = float(np.median(exposure[start_index:end_index]))
            local_difference = float(np.median(frame_difference[start_index:end_index]))
            head_score = 1.0 if edge_guard == 0 else min(1.0, start_seconds / edge_guard)
            tail_score = 1.0 if edge_guard == 0 else min(1.0, max(0.0, duration - end_seconds) / edge_guard)
            edge_score = min(head_score, tail_score)
            center_fraction = ((start_seconds + end_seconds) / 2) / max(duration, 0.001)
            position_score = max(0.0, 1.0 - abs(center_fraction - 0.45) / 0.55)
            stability_score = normalize_score(local_jitter, 0.35, 4.0)
            sharpness_score = max(0.0, min(1.0, np.log1p(local_sharpness) / np.log1p(900.0)))
            exposure_score = 1.0 - min(1.0, local_exposure * 2.0)
            continuity_score = normalize_score(local_difference, 5.0, 38.0)
            score = 100.0 * (
                0.42 * stability_score
                + 0.18 * sharpness_score
                + 0.10 * exposure_score
                + 0.10 * continuity_score
                + 0.15 * edge_score
                + 0.05 * position_score
            )
            candidates.append(
                {
                    "source_in": round(start_seconds, 3),
                    "source_out": round(end_seconds, 3),
                    "duration": round(end_seconds - start_seconds, 3),
                    "quality_score": round(score, 2),
                    "jitter": round(local_jitter, 3),
                    "sharpness": round(local_sharpness, 2),
                    "edge_score": round(edge_score, 3),
                }
            )
        best = max(candidates, key=lambda item: (item["quality_score"], item["edge_score"], -item["source_in"]))
        best_windows.append({"requested_duration": requested, **best})

    return {
        "source": str(source.resolve()),
        "duration_seconds": round(duration, 3),
        "storyboard": str(storyboard_path.resolve()),
        "sample_fps": sample_fps,
        "best_windows": best_windows,
        "whole_clip": {
            "median_jitter": round(float(np.median(jitter)), 3),
            "median_sharpness": round(float(np.median(sharpness)), 2),
            "median_exposure": round(float(np.median(exposure)), 4),
        },
    }


def analyze_broll(
    project: Path,
    work: Path,
    sample_fps: float,
    workers: int,
) -> dict[str, Any]:
    inventory = ensure_inventory(project, work)
    ffmpeg = find_ffmpeg()
    storyboard_dir = work / "broll_storyboards"
    storyboard_dir.mkdir(parents=True, exist_ok=True)
    requested_durations = (2.0, 2.8, 3.6, 4.5)
    clips: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                analyze_clip_quality,
                media,
                ffmpeg,
                storyboard_dir,
                sample_fps,
                requested_durations,
            ): media
            for media in inventory["b_roll"]
        }
        for future in as_completed(futures):
            media = futures[future]
            try:
                clips.append(future.result())
            except Exception as exc:
                failures.append({"source": media["path"], "error": str(exc)})
    clips.sort(key=lambda item: Path(item["source"]).name.lower())
    result = {
        "schema_version": 2,
        "method": "multi-frame phase-shift stability and image-quality windows",
        "clips": clips,
        "failures": failures,
    }
    write_json(work / "broll_analysis.json", result)
    return result


def transcribe_aroll(
    project: Path,
    work: Path,
    model_name: str,
    language: str,
    device: str,
    compute_type: str,
) -> dict[str, Any]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("缺少 faster-whisper。请先运行 scripts/setup.ps1。") from exc

    inventory = ensure_inventory(project, work)
    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    transcript_path = work / "transcript.json"
    existing_by_source: dict[str, dict[str, Any]] = {}
    if transcript_path.exists():
        existing = load_json(transcript_path)
        if existing.get("model") == model_name:
            existing_by_source = {
                str(Path(item["source"]).resolve()).lower(): item
                for item in existing.get("files", [])
                if item.get("segments")
            }
    files = []
    for media in inventory["a_roll"]:
        source = Path(media["path"])
        source_key = str(source.resolve()).lower()
        if source_key in existing_by_source:
            files.append(existing_by_source[source_key])
            continue
        segments_iter, info = model.transcribe(
            str(source),
            language=language,
            beam_size=5,
            vad_filter=True,
            word_timestamps=True,
            condition_on_previous_text=False,
        )
        segments = []
        for segment in segments_iter:
            segments.append(
                {
                    "start": round(float(segment.start), 3),
                    "end": round(float(segment.end), 3),
                    "text": segment.text.strip(),
                    "words": [
                        {
                            "start": round(float(word.start), 3) if word.start is not None else None,
                            "end": round(float(word.end), 3) if word.end is not None else None,
                            "word": word.word,
                            "probability": round(float(word.probability), 4),
                        }
                        for word in (segment.words or [])
                    ],
                }
            )
        files.append(
            {
                "source": str(source),
                "detected_language": info.language,
                "language_probability": round(float(info.language_probability), 4),
                "segments": segments,
            }
        )
        write_json(transcript_path, {"schema_version": 1, "model": model_name, "files": files})
    result = {"schema_version": 1, "model": model_name, "files": files}
    write_json(transcript_path, result)
    return result


def normalize_text(text: str) -> str:
    if _OPENCC is not None:
        text = _OPENCC.convert(text)
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", text).lower()


def _plain_script_lines(path: Path) -> list[tuple[str, str, bool]]:
    text = path.read_text(encoding="utf-8-sig")
    lines = []
    for raw in text.splitlines():
        value = raw.strip()
        if not value:
            continue
        is_markdown_heading = bool(re.match(r"^#{1,6}\s+", value))
        cleaned = re.sub(r"^#{1,6}\s+", "", value)
        cleaned = cleaned.replace("**", "").strip()
        inline_name = re.match(r"^([\u4e00-\u9fffA-Za-z·]{2,8})\s{2,}(.+)$", cleaned)
        if inline_name:
            lines.append((inline_name.group(1), "inline-speaker", True))
            lines.append((inline_name.group(2).strip(), "", False))
            continue
        short_plain_name = bool(
            re.fullmatch(r"[\u4e00-\u9fffA-Za-z·]{2,6}", cleaned)
            and not any(word in cleaned for word in ("视频脚本", "文字稿", "拍摄脚本"))
        )
        explicit = is_markdown_heading or short_plain_name
        lines.append((cleaned, "markdown-heading" if is_markdown_heading else "", explicit))
    return lines


def _docx_script_lines(path: Path) -> list[tuple[str, str, bool]]:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    ns = {"w": namespace}
    with ZipFile(path, "r") as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    raw: list[tuple[str, str]] = []
    for paragraph in root.findall(".//w:body/w:p", ns):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns)).strip()
        if not text:
            continue
        style_node = paragraph.find("./w:pPr/w:pStyle", ns)
        style = style_node.get(f"{{{namespace}}}val", "") if style_node is not None else ""
        raw.append((text, style))

    # Word templates often use custom/numeric style ids.  Learn the speaker
    # heading style from short lines ending in a Chinese/ASCII colon, then use
    # it for headings such as "包姐" that omit the colon.
    speaker_styles = {
        style
        for text, style in raw
        if style and re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9·（）()]{1,12}[：:]", text.strip())
    }
    return [(text, style, bool(style and style in speaker_styles)) for text, style in raw]


def _speaker_name(text: str, explicit_heading: bool) -> str | None:
    cleaned = re.sub(r"^#{1,6}\s+", "", text).replace("**", "").strip()
    without_colon = cleaned.rstrip("：:").strip()
    if any(word in without_colon for word in ("视频脚本", "文字稿", "拍摄脚本")):
        return None
    short_name = bool(re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9·（）()]{1,12}", without_colon))
    if short_name and (explicit_heading or cleaned.endswith(("：", ":"))):
        return without_colon
    return None


def script_sections(path: Path) -> list[dict[str, str]]:
    lines = _docx_script_lines(path) if path.suffix.lower() == ".docx" else _plain_script_lines(path)
    sections: list[dict[str, str]] = []
    current_speaker = "未标注人物"
    for text, _style, explicit_heading in lines:
        speaker = _speaker_name(text, explicit_heading)
        if speaker:
            current_speaker = speaker
            continue
        # Skip a document title before the first real speaker rather than
        # treating it as spoken copy.
        if current_speaker == "未标注人物" and any(
            word in text for word in ("视频脚本", "文字稿", "拍摄脚本")
        ):
            continue
        sections.append({"speaker": current_speaker, "text": text})
    if not sections:
        raise ValueError("讲稿没有可用段落。请确保人物标题和正文分别成段。")
    return sections


def script_paragraphs(path: Path) -> list[str]:
    return [section["text"] for section in script_sections(path)]


@dataclass(frozen=True)
class Candidate:
    file_index: int
    source: str
    start_segment: int
    end_segment: int
    global_start: int
    global_end: int
    start: float
    end: float
    text: str
    score: float


def candidate_score(target: str, observed: str) -> float:
    a, b = normalize_text(target), normalize_text(observed)
    if not a or not b:
        return 0.0
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    coverage = matched / max(len(a), 1)
    length_score = 1.0 - min(abs(len(a) - len(b)) / max(len(a), len(b), 1), 1.0)
    return round(0.55 * matcher.ratio() + 0.30 * coverage + 0.15 * length_score, 6)


def build_candidates(paragraph: str, transcript: dict[str, Any], limit: int = 50) -> list[Candidate]:
    target_len = len(normalize_text(paragraph))
    minimum = max(4, int(target_len * 0.42))
    maximum = max(24, int(target_len * 1.9))
    candidates: list[Candidate] = []
    global_offset = 0
    for file_index, file_data in enumerate(transcript["files"]):
        segments = file_data["segments"]
        for start_idx in range(len(segments)):
            combined = ""
            for end_idx in range(start_idx, min(len(segments), start_idx + 14)):
                combined += segments[end_idx]["text"]
                length = len(normalize_text(combined))
                if length >= minimum:
                    score = candidate_score(paragraph, combined)
                    if score >= 0.2:
                        candidates.append(
                            Candidate(
                                file_index=file_index,
                                source=file_data["source"],
                                start_segment=start_idx,
                                end_segment=end_idx,
                                global_start=global_offset + start_idx,
                                global_end=global_offset + end_idx,
                                start=float(segments[start_idx]["start"]),
                                end=float(segments[end_idx]["end"]),
                                text=combined.strip(),
                                score=score,
                            )
                        )
                if length > maximum:
                    break
        global_offset += len(segments) + 1
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[:limit]


def select_monotonic(paragraphs: list[str], transcript: dict[str, Any]) -> list[Candidate | None]:
    all_candidates = [build_candidates(paragraph, transcript) for paragraph in paragraphs]
    beam: list[tuple[float, int, list[Candidate | None]]] = [(0.0, -1, [])]
    for candidates in all_candidates:
        next_beam: list[tuple[float, int, list[Candidate | None]]] = []
        for total, last_end, choices in beam:
            next_beam.append((total - 0.35, last_end, choices + [None]))
            for cand in candidates:
                if cand.global_start > last_end:
                    next_beam.append((total + cand.score, cand.global_end, choices + [cand]))
        next_beam.sort(key=lambda item: item[0], reverse=True)
        beam = next_beam[:120]
    return beam[0][2]


def candidates_conflict(candidate: Candidate, selected: list[Candidate]) -> bool:
    for other in selected:
        if candidate.source != other.source:
            continue
        overlap = min(candidate.end, other.end) - max(candidate.start, other.start)
        if overlap > 0.35:
            return True
    return False


def select_content_matches(
    sections: list[dict[str, str]],
    transcript: dict[str, Any],
) -> tuple[list[Candidate | None], list[float], dict[str, dict[str, Any]]]:
    """Match shuffled A-roll globally, without relying on filenames or filming order."""
    candidate_sets = [build_candidates(section["text"], transcript) for section in sections]
    source_speaker_scores: dict[str, dict[str, float]] = {}
    for section, candidates in zip(sections, candidate_sets):
        speaker = section["speaker"]
        for candidate in candidates:
            scores = source_speaker_scores.setdefault(candidate.source, {})
            scores[speaker] = max(scores.get(speaker, 0.0), candidate.score)
    source_speakers: dict[str, dict[str, Any]] = {}
    for source, scores in source_speaker_scores.items():
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        best_speaker, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = best_score - second_score
        source_speakers[source] = {
            "speaker": best_speaker if best_score >= 0.38 and margin >= 0.08 else None,
            "score": round(best_score, 4),
            "margin": round(margin, 4),
        }
    order = sorted(
        range(len(sections)),
        key=lambda index: candidate_sets[index][0].score if candidate_sets[index] else 0.0,
        reverse=True,
    )
    selected: list[Candidate] = []
    choices: list[Candidate | None] = [None] * len(sections)
    for index in order:
        expected_speaker = sections[index]["speaker"]
        choice = next(
            (
                candidate
                for candidate in candidate_sets[index]
                if source_speakers.get(candidate.source, {}).get("speaker") in (None, expected_speaker)
                and not candidates_conflict(candidate, selected)
            ),
            None,
        )
        choices[index] = choice
        if choice is not None:
            selected.append(choice)

    speaker_margins: list[float] = []
    for index, choice in enumerate(choices):
        if choice is None:
            speaker_margins.append(0.0)
            continue
        other_speaker_scores = [
            candidate_score(section["text"], choice.text)
            for other_index, section in enumerate(sections)
            if other_index != index and section["speaker"] != sections[index]["speaker"]
        ]
        second_speaker_score = max(other_speaker_scores, default=0.0)
        speaker_margins.append(round(choice.score - second_speaker_score, 4))
    return choices, speaker_margins, source_speakers


def is_directing_chatter(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return True
    exact = {
        "好",
        "开始",
        "好开始",
        "等一下",
        "稍等",
        "重新录一下",
        "没问题",
        "非常好",
        "后期你们会剪辑吗",
        "后期会剪辑吗",
        "可以剪辑吗",
        "ok",
    }
    # Only standalone preparation language: do not discard a business sentence
    # such as 我开始使用AI之后效率提高了.
    preparation = bool(re.fullmatch(r'(?:嗯|啊|呃|好|好的)*(?:我|我们)?(?:开始了|开始吧|可以开始了)', normalized))
    return preparation or normalized in exact or (
        len(normalized) <= 36
        and any(
            phrase in normalized
            for phrase in (
                "重来",
                "重新录",
                "后期会剪",
                "要不要重新",
                "等一下",
                "声音是不是",
                "可以可以没问题",
                "要不要原则",
                "结束了",
            )
        )
    )


def transcript_sentences_for_sources(
    transcript: dict[str, Any],
    sources: set[str],
    target_chars: int = 38,
) -> list[dict[str, Any]]:
    sentences: list[dict[str, Any]] = []
    for file_data in transcript.get("files", []):
        source = str(file_data["source"])
        if source not in sources:
            continue
        buffer: list[str] = []
        buffer_probabilities: list[float] = []
        buffer_start: float | None = None
        buffer_end = 0.0
        for segment in file_data.get("segments", []):
            text = str(segment.get("text") or "").strip()
            if is_directing_chatter(text):
                # A removed segment is a real audio gap. Never concatenate text
                # across it while retaining one continuous source range.
                if buffer and buffer_start is not None:
                    sentences.append({'text': ''.join(buffer).rstrip('。！？!?；;')+'。',
                                      'source':source, 'source_in':round(buffer_start,3),
                                      'source_out':round(buffer_end,3),
                                      'confidence':round(sum(buffer_probabilities)/len(buffer_probabilities),4)
                                      if buffer_probabilities else 0.0})
                buffer, buffer_probabilities, buffer_start = [], [], None
                continue
            if buffer_start is None:
                buffer_start = float(segment["start"])
            buffer.append(text)
            buffer_probabilities.extend(
                float(word["probability"])
                for word in segment.get("words", [])
                if word.get("probability") is not None
            )
            buffer_end = float(segment["end"])
            combined = "".join(buffer).strip()
            terminal = bool(re.search(r"[。！？!?；;]$", text))
            if len(normalize_text(combined)) >= target_chars or terminal:
                sentences.append(
                    {
                        "text": combined.rstrip("。！？!?；;") + "。",
                        "source": source,
                        "source_in": round(buffer_start, 3),
                        "source_out": round(buffer_end, 3),
                        "confidence": round(
                            sum(buffer_probabilities) / len(buffer_probabilities), 4
                        )
                        if buffer_probabilities
                        else 0.0,
                    }
                )
                buffer = []
                buffer_probabilities = []
                buffer_start = None
        if buffer and buffer_start is not None:
            combined = "".join(buffer).strip()
            if len(normalize_text(combined)) >= 6:
                sentences.append(
                    {
                        "text": combined.rstrip("。！？!?；;") + "。",
                        "source": source,
                        "source_in": round(buffer_start, 3),
                        "source_out": round(buffer_end, 3),
                        "confidence": round(
                            sum(buffer_probabilities) / len(buffer_probabilities), 4
                        )
                        if buffer_probabilities
                        else 0.0,
                    }
                )

    deduplicated: list[dict[str, Any]] = []
    for sentence in sentences:
        if deduplicated and candidate_score(deduplicated[-1]["text"], sentence["text"]) >= 0.86:
            if len(normalize_text(sentence["text"])) > len(normalize_text(deduplicated[-1]["text"])):
                deduplicated[-1] = sentence
            continue
        deduplicated.append(sentence)
    return deduplicated


def load_speaker_map(path: Path | None) -> tuple[dict[str, str], list[str]]:
    if path is None:
        return {}, []
    data = load_json(path)
    sources = {str(name).lower(): str(speaker) for name, speaker in data.get("sources", {}).items()}
    priority = [str(speaker) for speaker in data.get("speaker_priority", [])]
    return sources, priority


def load_transcript_corrections(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    data = load_json(path)
    return {str(source): str(target) for source, target in data.get("replacements", {}).items()}


def clean_transcript_sentence(text: str, corrections: dict[str, str]) -> str:
    if _OPENCC is not None:
        text = _OPENCC.convert(text)
    for source, target in corrections.items():
        text = text.replace(source, target)
    text = text.strip()
    previous = None
    while text != previous:
        previous = text
        text = re.sub(
            r"^(?:嗯|啊|呃|好\s*开始吧|好\s*我开始了|开始吧|稍等|那个|那么|好)[\s,，、]*",
            "",
            text,
        )
    text = re.sub(
        r"(?:好\s*OK|OK吗|可以可以没问题|没问题|要不要重新录.*|后期你们会剪辑吗|后期会剪辑吗)$",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s+", " ", text).strip()
    if not text or is_directing_chatter(text):
        return ""
    return text.rstrip("。！？!?；;") + "。"


def transcript_role_score(text: str) -> int:
    roles = (
        ("董事长", 100),
        ("总裁", 95),
        ("副总裁", 90),
        ("总经理", 85),
        ("负责人", 80),
        ("经理", 70),
        ("设计师", 40),
    )
    return max((score for keyword, score in roles if keyword in text), default=0)


def build_v1_from_transcript(
    work: Path,
    speaker_map_path: Path | None,
    corrections_path: Path | None,
    duplicate_threshold: float,
    sentence_target_chars: int,
) -> dict[str, Any]:
    transcript_path = work / "transcript.json"
    if not transcript_path.exists():
        raise FileNotFoundError("没有找到 transcript.json，请先运行 transcribe。")
    transcript = load_json(transcript_path)
    explicit_map, explicit_priority = load_speaker_map(speaker_map_path)
    corrections = load_transcript_corrections(corrections_path)
    source_to_speaker: dict[str, str] = {}
    source_texts: dict[str, str] = {}
    for file_data in transcript.get("files", []):
        source = str(file_data["source"])
        name = Path(source).name
        source_texts[source] = "".join(str(segment.get("text") or "") for segment in file_data.get("segments", []))
        source_to_speaker[source] = explicit_map.get(name.lower(), Path(source).stem)

    speakers = list(dict.fromkeys(source_to_speaker.values()))
    if explicit_priority:
        priority = explicit_priority + [speaker for speaker in speakers if speaker not in explicit_priority]
    else:
        role_scores = {
            speaker: max(
                (
                    transcript_role_score(source_texts[source])
                    for source, assigned in source_to_speaker.items()
                    if assigned == speaker
                ),
                default=0,
            )
            for speaker in speakers
        }
        priority = sorted(speakers, key=lambda speaker: (-role_scores[speaker], speakers.index(speaker)))
    ranks = {speaker: index for index, speaker in enumerate(priority)}

    raw_by_speaker: dict[str, list[dict[str, Any]]] = {speaker: [] for speaker in priority}
    for source, speaker in source_to_speaker.items():
        raw_by_speaker[speaker].extend(
            transcript_sentences_for_sources(
                transcript,
                {source},
                target_chars=sentence_target_chars,
            )
        )

    deduplicated_by_speaker: dict[str, list[dict[str, Any]]] = {}
    duplicate_log: list[dict[str, Any]] = []
    for speaker in priority:
        kept: list[dict[str, Any]] = []
        for sentence in raw_by_speaker.get(speaker, []):
            sentence = dict(sentence)
            sentence["text"] = clean_transcript_sentence(sentence["text"], corrections)
            normalized = normalize_text(sentence["text"])
            if len(normalized) < 8:
                continue
            duplicate_index = None
            duplicate_score = 0.0
            for index, existing in enumerate(kept):
                lexical = max(
                    candidate_score(existing["text"], sentence["text"]),
                    char_bigram_similarity(existing["text"], sentence["text"]),
                )
                shorter, longer = sorted(
                    (normalize_text(existing["text"]), normalized), key=len
                )
                containment = len(shorter) >= 12 and shorter in longer
                if lexical >= duplicate_threshold or containment:
                    duplicate_index = index
                    duplicate_score = lexical
                    break
            if duplicate_index is None:
                kept.append(sentence)
                continue
            existing = kept[duplicate_index]
            existing_quality = float(existing.get("confidence") or 0.0) + min(
                len(normalize_text(existing["text"])), 60
            ) / 600
            new_quality = float(sentence.get("confidence") or 0.0) + min(len(normalized), 60) / 600
            replacement = new_quality > existing_quality
            if replacement:
                kept[duplicate_index] = sentence
            duplicate_log.append(
                {
                    "speaker": speaker,
                    "kept": sentence["text"] if replacement else existing["text"],
                    "removed": existing["text"] if replacement else sentence["text"],
                    "similarity": round(duplicate_score, 4),
                }
            )
        deduplicated_by_speaker[speaker] = kept

    output_sentences = []
    order = 0
    for speaker in priority:
        for sentence in deduplicated_by_speaker.get(speaker, []):
            order += 1
            output_sentences.append(
                {
                    "order": order,
                    "speaker": speaker,
                    "speaker_rank": ranks[speaker],
                    "text": sentence["text"],
                    "origin": "transcript",
                    "protected_data": has_specific_data(sentence["text"]),
                    "source": sentence["source"],
                    "source_in": sentence["source_in"],
                    "source_out": sentence["source_out"],
                    "transcription_confidence": sentence.get("confidence", 0.0),
                }
            )
    result = {
        "schema_version": 2,
        "strategy": "transcript-only deduplication; V0 content ignored",
        "speaker_order": priority,
        "speaker_map": source_to_speaker,
        "duplicate_threshold": duplicate_threshold,
        "sentence_target_chars": sentence_target_chars,
        "corrections": str(corrections_path.resolve()) if corrections_path else None,
        "raw_sentence_count": sum(len(items) for items in raw_by_speaker.values()),
        "deduplicated_sentence_count": len(output_sentences),
        "duplicates_removed": duplicate_log,
        "sentences": output_sentences,
    }
    write_json(work / "script_v1.json", result)
    markdown = ["# 文稿 V1", ""]
    for speaker in priority:
        markdown.extend([f"## {speaker}", ""])
        for sentence in output_sentences:
            if sentence["speaker"] == speaker:
                markdown.extend([sentence["text"], ""])
    (work / "script_v1.md").write_text("\n".join(markdown), encoding="utf-8")
    write_json(work / "script_v1_dedup_report.json", {"duplicates_removed": duplicate_log})
    return result


def prepare_v1_correction(work: Path, confidence_threshold: float) -> dict[str, Any]:
    """Create a context-rich worksheet for semantic ASR proofreading.

    Every row retains the immutable V1 order and time anchors.  The language
    model sees neighbouring rows because accented speech is often only
    recoverable from the surrounding idea, not from the isolated ASR chunk.
    """

    v1_path = work / "script_v1.json"
    if not v1_path.exists():
        raise FileNotFoundError("没有找到 script_v1.json，请先运行 build-v1。")
    v1 = load_json(v1_path)
    sentences = list(v1.get("sentences", []))
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(sentences):
        previous = sentences[index - 1] if index > 0 else None
        following = sentences[index + 1] if index + 1 < len(sentences) else None
        same_previous = previous if previous and previous["speaker"] == item["speaker"] else None
        same_following = following if following and following["speaker"] == item["speaker"] else None
        confidence = float(item.get("transcription_confidence") or 0.0)
        flags: list[str] = []
        if confidence < confidence_threshold:
            flags.append("low_asr_confidence")
        if _dependent_start(item["text"]):
            flags.append("depends_on_previous_context")
        if _incomplete_end(item["text"]):
            flags.append("continues_into_next_context")
        if _noisy_context(item["text"]):
            flags.append("possible_directing_chatter")
        rows.append(
            {
                "order": int(item["order"]),
                "speaker": item["speaker"],
                "source": item["source"],
                "source_in": item["source_in"],
                "source_out": item["source_out"],
                "transcription_confidence": confidence,
                "priority": "high" if flags else "normal",
                "flags": flags,
                "previous_text": same_previous["text"] if same_previous else None,
                "original_text": item["text"],
                "next_text": same_following["text"] if same_following else None,
            }
        )
    result = {
        "schema_version": 1,
        "source_v1": str(v1_path.resolve()),
        "policy": (
            "只修正有上下文或复听依据的同音错字、专有名词、漏词和断句；"
            "不得补写客户没有说过的观点或数据。"
        ),
        "confidence_threshold": confidence_threshold,
        "rows": rows,
    }
    write_json(work / "v1_correction_worksheet.json", result)
    markdown = [
        "# V1 语句校对清单",
        "",
        "逐句查看前后文。能确定时写入 `v1_correction_plan.json`；不能确定时保留原文并标记复听。",
        "",
    ]
    for row in rows:
        marker = "高" if row["priority"] == "high" else "常规"
        markdown.extend(
            [
                f"## 第 {row['order']} 句｜{row['speaker']}｜优先级：{marker}",
                "",
                f"- 时间：`{row['source_in']}`–`{row['source_out']}`",
                f"- 前文：{row['previous_text'] or '（无）'}",
                f"- 原始转写：{row['original_text']}",
                f"- 后文：{row['next_text'] or '（无）'}",
                f"- 提示：{', '.join(row['flags']) if row['flags'] else '常规语义校对'}",
                "",
            ]
        )
    (work / "v1_correction_worksheet.md").write_text(
        "\n".join(markdown), encoding="utf-8"
    )
    return result


def apply_v1_corrections(
    work: Path,
    review_path: Path,
    min_context_similarity: float,
) -> dict[str, Any]:
    """Apply reviewed ASR corrections without changing source anchors."""

    v1_path = work / "script_v1.json"
    if not v1_path.exists():
        raise FileNotFoundError("没有找到 script_v1.json，请先运行 build-v1。")
    raw = load_json(v1_path)
    review = load_json(review_path)
    edits = list(review.get("edits", []))
    by_order = {int(item["order"]): dict(item) for item in raw.get("sentences", [])}
    seen: set[int] = set()
    applied: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = list(review.get("needs_audio_review", []))
    errors: list[str] = []

    for edit in edits:
        order = int(edit.get("order", 0))
        if order in seen:
            errors.append(f"第 {order} 句出现重复校正")
            continue
        seen.add(order)
        item = by_order.get(order)
        if item is None:
            errors.append(f"校正计划引用了不存在的第 {order} 句")
            continue
        original = str(item["text"])
        expected_original = str(edit.get("original_text") or original)
        if normalize_text(expected_original) != normalize_text(original):
            errors.append(f"第 {order} 句原文已变化，请重新生成校正计划")
            continue
        corrected = str(edit.get("corrected_text") or "").strip()
        if not corrected:
            errors.append(f"第 {order} 句 corrected_text 为空")
            continue
        corrected = corrected.rstrip("。！？!?；;") + "。"
        evidence = str(edit.get("evidence") or "context").strip().lower()
        similarity = SequenceMatcher(
            None,
            normalize_text(original),
            normalize_text(corrected),
            autojunk=False,
        ).ratio()
        if evidence not in {"context", "audio_relisten", "second_asr"}:
            errors.append(f"第 {order} 句 evidence 必须是 context、audio_relisten 或 second_asr")
            continue
        threshold = min_context_similarity if evidence == "context" else 0.25
        if similarity < threshold:
            errors.append(
                f"第 {order} 句改动过大（相似度 {similarity:.3f}，最低 {threshold:.3f}）"
            )
            continue
        original_data = data_fingerprints(original)
        corrected_data = data_fingerprints(corrected)
        if corrected_data - original_data and evidence == "context":
            errors.append(
                f"第 {order} 句仅凭上下文新增了数据："
                + "、".join(sorted(corrected_data - original_data))
            )
            continue
        item["raw_text"] = original
        item["text"] = corrected
        item["protected_data"] = has_specific_data(corrected)
        item["correction"] = {
            "evidence": evidence,
            "confidence": float(edit.get("confidence", 0.0)),
            "change_types": list(edit.get("change_types", [])),
            "reason": str(edit.get("reason") or ""),
            "similarity_to_raw": round(similarity, 4),
        }
        by_order[order] = item
        applied.append(
            {
                "order": order,
                "speaker": item["speaker"],
                "original_text": original,
                "corrected_text": corrected,
                "evidence": evidence,
                "similarity_to_raw": round(similarity, 4),
            }
        )

    if errors:
        raise ValueError("V1语句校正计划未通过安全检查：\n- " + "\n- ".join(errors))

    corrected_sentences = [by_order[int(item["order"])] for item in raw.get("sentences", [])]
    result = dict(raw)
    result.update(
        {
            "schema_version": 3,
            "strategy": "transcript-only deduplication + traceable ASR sentence correction",
            "source_v1": str(v1_path.resolve()),
            "correction_plan": str(review_path.resolve()),
            "corrections_applied": len(applied),
            "unresolved_audio_reviews": unresolved,
            "ready_for_v2": not unresolved,
            "sentences": corrected_sentences,
        }
    )
    corrected_path = work / "script_v1_corrected.json"
    write_json(corrected_path, result)
    markdown = ["# 文稿 V1（语句校正版）", ""]
    for speaker in raw.get("speaker_order", []):
        markdown.extend([f"## {speaker}", ""])
        for sentence in corrected_sentences:
            if sentence["speaker"] == speaker:
                markdown.extend([sentence["text"], ""])
    (work / "script_v1_corrected.md").write_text(
        "\n".join(markdown), encoding="utf-8"
    )
    report_lines = [
        "# V1 语句校正报告",
        "",
        f"- 已应用校正：{len(applied)} 句",
        f"- 仍需复听：{len(unresolved)} 句",
        f"- 可进入 V2：{'是' if not unresolved else '否'}",
        "",
    ]
    for item in applied:
        report_lines.extend(
            [
                f"## 第 {item['order']} 句｜{item['speaker']}",
                "",
                f"- 原文：{item['original_text']}",
                f"- 校正：{item['corrected_text']}",
                f"- 依据：{item['evidence']}；与原文相似度 {item['similarity_to_raw']:.3f}",
                "",
            ]
        )
    for item in unresolved:
        report_lines.append(
            f"- 需复听：第 {item.get('order')} 句｜{item.get('reason', '上下文不足')}"
        )
    (work / "v1_correction_report.md").write_text(
        "\n".join(report_lines), encoding="utf-8"
    )
    return result


def has_specific_data(text: str) -> bool:
    normalized = text.replace(" ", "")
    return bool(
        re.search(r"\d", normalized)
        or re.search(
            r"(?<!第)(?:零|一|二|两|三|四|五|六|七|八|九|十|百|千|万|半)+"
            r"(?:年|月|天|小时|分钟|秒|方|平方米|套|家|单|倍|成|人|万|元|%)",
            normalized,
        )
        or "百分之" in normalized
    )


def char_bigram_similarity(left: str, right: str) -> float:
    a, b = normalize_text(left), normalize_text(right)
    if len(a) < 2 or len(b) < 2:
        return candidate_score(a, b)
    aa = {a[index : index + 2] for index in range(len(a) - 1)}
    bb = {b[index : index + 2] for index in range(len(b) - 1)}
    return 2 * len(aa & bb) / max(1, len(aa) + len(bb))


def sentence_topics(text: str) -> set[str]:
    groups = {
        "training": ("培训", "上手", "操作", "教学"),
        "speed": ("效率", "分钟", "小时", "天", "速度", "快速", "周期"),
        "communication": ("沟通", "需求", "邀约", "谈单", "到店", "信任"),
        "budget": ("预算", "报价", "增项", "价格"),
        "construction": ("施工", "工地", "放样", "落地"),
        "design": ("效果图", "风格", "设计师", "方案", "户型"),
        "result": ("成交", "定金", "提升", "压缩", "认可", "反馈", "赢单"),
    }
    return {name for name, keywords in groups.items() if any(keyword in text for keyword in keywords)}


def _canonical_chinese_number(token: str) -> str:
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if token.isdigit():
        return str(int(token))
    if "十" in token and all(character in digits or character == "十" for character in token):
        left, right = token.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return str(tens * 10 + ones)
    if all(character in digits for character in token):
        return "".join(str(digits[character]) for character in token)
    return token


def data_fingerprints(text: str) -> set[str]:
    """Return comparable data points so 14万 and 十四万 count as the same fact."""
    compact = re.sub(r"\s+", "", text)
    number = r"(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十]+)"
    unit = r"(?:平方米|小时|分钟|年|月|天|秒|方|套|家|单|倍|成|人|万|元|%)"
    points: set[str] = set()
    occupied: list[tuple[int, int]] = []
    for match in re.finditer(rf"(?<!第)({number})(?:到|至|[-—])({number})({unit})", compact):
        prefix = compact[max(0, match.start() - 3) : match.start()]
        if prefix.endswith(("第", "隔了", "过了")):
            continue
        points.add(
            f"{_canonical_chinese_number(match.group(1))}-{_canonical_chinese_number(match.group(2))}{match.group(3)}"
        )
        occupied.append(match.span())
    for match in re.finditer(rf"(?<!第)({number})({unit})", compact):
        if any(start <= match.start() and match.end() <= end for start, end in occupied):
            continue
        prefix = compact[max(0, match.start() - 3) : match.start()]
        if prefix.endswith(("第", "隔了", "过了")):
            continue
        if match.group(0) in {"一方", "两方"} and compact[match.end() : match.end() + 1] in {"面", "都"}:
            # "一方面" and "对双方都有帮助" are not area measurements.
            continue
        points.add(f"{_canonical_chinese_number(match.group(1))}{match.group(2)}")
    for match in re.finditer(rf"百分之({number})", compact):
        points.add(f"{_canonical_chinese_number(match.group(1))}%")
    return points


_DEPENDENT_STARTS = (
    "他",
    "她",
    "它",
    "这个",
    "这种",
    "这些",
    "这样",
    "那我们",
    "那对",
    "那么",
    "所以",
    "然后",
    "另外",
    "因为",
    "因此",
    "或者",
    "看到之后",
    "给到客户",
    "给我们",
    "跟客户",
    "长期的",
    "差不多",
    "掌握客户",
    "都是要",
    "就是",
    "特别是",
    "反正",
    "全员去使用",
    "刚需的客户来说",
)

_INCOMPLETE_ENDS = (
    "因为",
    "所以",
    "然后",
    "但是",
    "包括",
    "需要",
    "可以",
    "这样的话",
    "之后",
    "是因为",
    "一个",
    "一种",
    "给客户",
)

_CONTEXT_NOISE = (
    "会剪我先发",
    "下一页没关系",
    "稍等",
    "后期会剪",
    "重新录",
)


def _edge_text(text: str) -> str:
    return re.sub(r"[\s，。；：！？、,.!?;:]", "", text)


def _dependent_start(text: str) -> bool:
    compact = _edge_text(text)
    return compact.startswith(_DEPENDENT_STARTS)


def _incomplete_end(text: str) -> bool:
    compact = _edge_text(text)
    return compact.endswith(_INCOMPLETE_ENDS)


def _noisy_context(text: str) -> bool:
    compact = _edge_text(text)
    return any(marker in compact for marker in _CONTEXT_NOISE)


def _standalone_sentence(text: str) -> bool:
    compact = _edge_text(text)
    if _dependent_start(text) or _incomplete_end(text):
        return False
    if compact.startswith(("大家好", "我是", "无论", "我们都在拥抱", "酷家乐AI最大的价值")):
        return True
    if data_fingerprints(text) and any(
        keyword in compact
        for keyword in ("成立", "客单价", "整个过程", "压缩到", "提高到", "降低到", "从")
    ):
        return True
    return False


def _narratively_adjacent(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if int(right["order"]) != int(left["order"]) + 1:
        return False
    if left["speaker"] != right["speaker"] or left["source"] != right["source"]:
        return False
    gap = float(right.get("source_in") or 0.0) - float(left.get("source_out") or 0.0)
    return -0.1 <= gap <= 2.0


def _coherent_runs(
    sentences: list[dict[str, Any]], selected_orders: set[int]
) -> list[list[dict[str, Any]]]:
    selected = [item for item in sentences if item["order"] in selected_orders]
    runs: list[list[dict[str, Any]]] = []
    for item in selected:
        if runs and _narratively_adjacent(runs[-1][-1], item):
            runs[-1].append(item)
        else:
            runs.append([item])
    return runs


def _selection_totals(
    sentences: list[dict[str, Any]], selected_orders: set[int]
) -> tuple[int, float]:
    chosen = [item for item in sentences if item["order"] in selected_orders]
    return (
        sum(int(item["char_count"]) for item in chosen),
        sum(float(item["source_duration_seconds"]) for item in chosen),
    )


def _coherence_defects(runs: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    defects: list[dict[str, Any]] = []
    for run in runs:
        first, last = run[0], run[-1]
        if _dependent_start(first["text"]):
            defects.append(
                {
                    "type": "dependent_start",
                    "order": first["order"],
                    "speaker": first["speaker"],
                    "text": first["text"],
                }
            )
        if _incomplete_end(last["text"]):
            defects.append(
                {
                    "type": "incomplete_end",
                    "order": last["order"],
                    "speaker": last["speaker"],
                    "text": last["text"],
                }
            )
        if len(run) == 1 and not _standalone_sentence(first["text"]):
            defects.append(
                {
                    "type": "isolated_fragment",
                    "order": first["order"],
                    "speaker": first["speaker"],
                    "text": first["text"],
                }
            )
    return defects


def repair_v2_coherence(
    sentences: list[dict[str, Any]],
    selected_orders: set[int],
    hard_required_orders: set[int],
    primary: str,
    min_chars: int,
    max_chars: int,
    min_seconds: float,
    max_seconds: float,
) -> tuple[set[int], dict[str, Any]]:
    """Repair fragmented V2 selection by adding context and pruning whole runs.

    V1 units are transcript chunks, not guaranteed grammatical sentences.  A
    high-scoring isolated chunk therefore cannot safely be treated as an
    independent paragraph.  This pass restores immediate dependencies, fills
    one-chunk holes, and, when the result becomes too long, removes complete
    low-value runs instead of punching new holes inside the narrative.
    """

    selected_orders = set(selected_orders)
    original_orders = set(selected_orders)
    order_to_index = {int(item["order"]): index for index, item in enumerate(sentences)}
    context_added: set[int] = set()
    bridged_orders: set[int] = set()

    def previous_of(item: dict[str, Any]) -> dict[str, Any] | None:
        index = order_to_index[int(item["order"])]
        if index <= 0:
            return None
        candidate = sentences[index - 1]
        return candidate if _narratively_adjacent(candidate, item) else None

    def next_of(item: dict[str, Any]) -> dict[str, Any] | None:
        index = order_to_index[int(item["order"])]
        if index + 1 >= len(sentences):
            return None
        candidate = sentences[index + 1]
        return candidate if _narratively_adjacent(item, candidate) else None

    for _ in range(4):
        additions: set[int] = set()
        runs = _coherent_runs(sentences, selected_orders)
        for run in runs:
            first, last = run[0], run[-1]
            previous = previous_of(first)
            following = next_of(last)
            if (
                previous is not None
                and previous["order"] not in selected_orders
                and not _noisy_context(previous["text"])
                and (_dependent_start(first["text"]) or _incomplete_end(previous["text"]))
            ):
                additions.add(int(previous["order"]))
            if (
                following is not None
                and following["order"] not in selected_orders
                and not _noisy_context(following["text"])
                and _incomplete_end(last["text"])
            ):
                additions.add(int(following["order"]))

        # A single missing transcript chunk between two retained chunks is
        # almost always more damaging than the small time saving it provides.
        for index in range(1, len(sentences) - 1):
            item = sentences[index]
            if item["order"] in selected_orders or _noisy_context(item["text"]):
                continue
            previous, following = sentences[index - 1], sentences[index + 1]
            if (
                previous["order"] in selected_orders
                and following["order"] in selected_orders
                and _narratively_adjacent(previous, item)
                and _narratively_adjacent(item, following)
            ):
                additions.add(int(item["order"]))
                bridged_orders.add(int(item["order"]))

        additions -= selected_orders
        if not additions:
            break
        selected_orders.update(additions)
        context_added.update(additions)

    removed_runs: list[dict[str, Any]] = []
    chars, seconds = _selection_totals(sentences, selected_orders)
    while chars > max_chars or seconds > max_seconds:
        runs = _coherent_runs(sentences, selected_orders)
        speaker_run_counts: dict[str, int] = {}
        for run in runs:
            speaker_run_counts[run[0]["speaker"]] = speaker_run_counts.get(run[0]["speaker"], 0) + 1
        removable = []
        for run in runs:
            orders = {int(item["order"]) for item in run}
            speaker = run[0]["speaker"]
            if orders & hard_required_orders or speaker_run_counts[speaker] <= 1:
                continue
            run_chars = sum(int(item["char_count"]) for item in run)
            run_seconds = sum(float(item["source_duration_seconds"]) for item in run)
            run_value = sum(float(item["importance_score"]) for item in run)
            removable.append(
                (
                    speaker == primary,
                    run_value / max(1.0, run_seconds),
                    run_value / max(1, run_chars),
                    run,
                )
            )
        if not removable:
            break
        _, _, _, run = min(removable, key=lambda value: value[:3])
        for item in run:
            selected_orders.remove(int(item["order"]))
        removed_runs.append(
            {
                "speaker": run[0]["speaker"],
                "orders": [int(item["order"]) for item in run],
                "reason": "remove_whole_low_value_run_to_fit_target",
            }
        )
        chars, seconds = _selection_totals(sentences, selected_orders)

    # Runs containing one protected data sentence can still be very long.
    # Remove a safe contiguous non-required span when doing so leaves both
    # sides as readable paragraph boundaries.  This is still whole-sentence
    # deletion; it never removes a protected data sentence.
    while chars > max_chars or seconds > max_seconds:
        runs = _coherent_runs(sentences, selected_orders)
        current_defects = len(_coherence_defects(runs))
        speaker_counts: dict[str, int] = {}
        for item in sentences:
            if item["order"] in selected_orders:
                speaker_counts[item["speaker"]] = speaker_counts.get(item["speaker"], 0) + 1
        span_candidates: list[
            tuple[tuple[float, ...], list[dict[str, Any]], set[int]]
        ] = []
        for run in runs:
            for start in range(len(run)):
                if int(run[start]["order"]) in hard_required_orders:
                    continue
                for end in range(start, len(run)):
                    span = run[start : end + 1]
                    orders = {int(item["order"]) for item in span}
                    if orders & hard_required_orders:
                        break
                    speaker = span[0]["speaker"]
                    if speaker_counts.get(speaker, 0) - len(span) < 1:
                        continue
                    trial = selected_orders - orders
                    trial_defects = len(
                        _coherence_defects(_coherent_runs(sentences, trial))
                    )
                    if trial_defects > current_defects:
                        continue
                    span_seconds = sum(
                        float(item["source_duration_seconds"]) for item in span
                    )
                    span_value = sum(float(item["importance_score"]) for item in span)
                    rank = (
                        1.0 if speaker == primary else 0.0,
                        span_value / max(1.0, span_seconds),
                        -span_seconds,
                    )
                    span_candidates.append((rank, span, orders))
        if not span_candidates:
            break
        _, span, orders = min(span_candidates, key=lambda value: value[0])
        selected_orders -= orders
        removed_runs.append(
            {
                "speaker": span[0]["speaker"],
                "orders": sorted(orders),
                "reason": "remove_safe_low_value_span_to_fit_target",
            }
        )
        chars, seconds = _selection_totals(sentences, selected_orders)

    # If required data and its context still exceed the ceiling, trim only
    # automatically added edge chunks whose removal does not introduce a new
    # dangling start/end.  Never remove an original selection here.
    if chars > max_chars or seconds > max_seconds:
        for order in sorted(
            context_added,
            key=lambda value: (
                sentences[order_to_index[value]]["importance_score"],
                -sentences[order_to_index[value]]["source_duration_seconds"],
            ),
        ):
            if order not in selected_orders or order in hard_required_orders:
                continue
            before_defects = len(_coherence_defects(_coherent_runs(sentences, selected_orders)))
            trial = selected_orders - {order}
            after_defects = len(_coherence_defects(_coherent_runs(sentences, trial)))
            if after_defects > before_defects:
                continue
            selected_orders = trial
            chars, seconds = _selection_totals(sentences, selected_orders)
            if chars <= max_chars and seconds <= max_seconds:
                break

    # Refill a short result with self-contained consecutive windows.  Adding a
    # whole window is less likely to create an abrupt jump than adding the best
    # isolated sentence.
    while chars < min_chars or seconds < min_seconds:
        candidates: list[tuple[tuple[float, ...], list[dict[str, Any]]]] = []
        for size in (1, 2, 3, 4):
            for start in range(0, len(sentences) - size + 1):
                window = sentences[start : start + size]
                orders = {int(item["order"]) for item in window}
                if orders & selected_orders:
                    continue
                if any(
                    not _narratively_adjacent(left, right)
                    for left, right in zip(window, window[1:])
                ):
                    continue
                if _dependent_start(window[0]["text"]) or _incomplete_end(window[-1]["text"]):
                    continue
                if size == 1 and not _standalone_sentence(window[0]["text"]):
                    continue
                add_chars = sum(int(item["char_count"]) for item in window)
                add_seconds = sum(float(item["source_duration_seconds"]) for item in window)
                if chars + add_chars > max_chars or seconds + add_seconds > max_seconds:
                    continue
                value = sum(float(item["importance_score"]) for item in window)
                rank = (
                    1.0 if window[0]["speaker"] == primary else 0.0,
                    value / max(1.0, add_seconds),
                    value,
                    -float(window[0]["order"]),
                )
                candidates.append((rank, window))
        if not candidates:
            break
        _, window = max(candidates, key=lambda value: value[0])
        selected_orders.update(int(item["order"]) for item in window)
        chars, seconds = _selection_totals(sentences, selected_orders)

    # Near the duration ceiling, adding another paragraph may not fit even
    # though the character target is only slightly short.  Exchange one safe
    # optional paragraph for a denser self-contained paragraph instead of
    # accepting a broken fragment merely to gain a few characters.
    while chars < min_chars:
        runs = _coherent_runs(sentences, selected_orders)
        current_defects = len(_coherence_defects(runs))
        selected_by_speaker: dict[str, int] = {}
        for item in sentences:
            if item["order"] in selected_orders:
                selected_by_speaker[item["speaker"]] = selected_by_speaker.get(item["speaker"], 0) + 1

        removal_options: list[list[dict[str, Any]]] = [[]]
        for run in runs:
            for start in range(len(run)):
                if int(run[start]["order"]) in hard_required_orders:
                    continue
                for end in range(start, len(run)):
                    span = run[start : end + 1]
                    if any(int(item["order"]) in hard_required_orders for item in span):
                        break
                    if selected_by_speaker.get(run[0]["speaker"], 0) <= len(span):
                        continue
                    removal_options.append(span)

        addition_options: list[list[dict[str, Any]]] = []
        for size in (1, 2, 3):
            for start in range(0, len(sentences) - size + 1):
                window = sentences[start : start + size]
                orders = {int(item["order"]) for item in window}
                if orders & selected_orders:
                    continue
                if any(
                    not _narratively_adjacent(left, right)
                    for left, right in zip(window, window[1:])
                ):
                    continue
                if _dependent_start(window[0]["text"]) or _incomplete_end(window[-1]["text"]):
                    continue
                if size == 1 and not _standalone_sentence(window[0]["text"]):
                    continue
                addition_options.append(window)

        best_exchange: tuple[
            tuple[float, ...], list[dict[str, Any]], list[dict[str, Any]], set[int]
        ] | None = None
        for removed in removal_options:
            removed_orders = {int(item["order"]) for item in removed}
            base = selected_orders - removed_orders
            for added in addition_options:
                added_orders = {int(item["order"]) for item in added}
                trial = base | added_orders
                new_chars, new_seconds = _selection_totals(sentences, trial)
                if new_chars <= chars or new_chars > max_chars or new_seconds > max_seconds:
                    continue
                defects = len(_coherence_defects(_coherent_runs(sentences, trial)))
                if defects > current_defects:
                    continue
                removed_value = sum(float(item["importance_score"]) for item in removed)
                added_value = sum(float(item["importance_score"]) for item in added)
                primary_delta = sum(
                    int(item["char_count"]) for item in added if item["speaker"] == primary
                ) - sum(
                    int(item["char_count"]) for item in removed if item["speaker"] == primary
                )
                rank = (
                    1.0 if new_chars >= min_chars else 0.0,
                    1.0 if primary_delta >= 0 else 0.0,
                    float(primary_delta),
                    added_value - removed_value,
                    float(new_chars - chars),
                    -abs(max_seconds - new_seconds),
                )
                if best_exchange is None or rank > best_exchange[0]:
                    best_exchange = (rank, removed, added, trial)
        if best_exchange is None:
            break
        _, removed, added, selected_orders = best_exchange
        removed_runs.append(
            {
                "speaker": removed[0]["speaker"] if removed else "",
                "orders": [int(item["order"]) for item in removed],
                "replacement_orders": [int(item["order"]) for item in added],
                "reason": "replace_with_denser_coherent_paragraph",
            }
        )
        chars, seconds = _selection_totals(sentences, selected_orders)

    runs = _coherent_runs(sentences, selected_orders)
    defects = _coherence_defects(runs)
    paragraph_by_order: dict[int, int] = {}
    for paragraph, run in enumerate(runs, start=1):
        for item in run:
            paragraph_by_order[int(item["order"])] = paragraph
    report = {
        "strategy": "context closure + single-gap bridging + whole-run pruning",
        "original_selected_count": len(original_orders),
        "final_selected_count": len(selected_orders),
        "context_orders_added": sorted(context_added & selected_orders),
        "single_gap_orders_bridged": sorted(bridged_orders & selected_orders),
        "removed_runs": removed_runs,
        "paragraph_count": len(runs),
        "paragraph_by_order": paragraph_by_order,
        "remaining_defects": defects,
        "requires_review": bool(defects),
    }
    return selected_orders, report


def apply_v2_editorial_plan(
    sentences: list[dict[str, Any]],
    plan_path: Path,
    primary: str,
    min_chars: int,
    max_chars: int,
    min_seconds: float,
    max_seconds: float,
) -> tuple[set[int], dict[str, Any], dict[str, Any]]:
    """Apply an AI-reviewed paragraph plan and enforce measurable constraints.

    Speech-to-text chunks are convenient time anchors, but they are not
    guaranteed to be grammatical sentences. The semantic review therefore
    chooses complete blocks of adjacent chunks. This function validates that
    plan, total duration, character count, paragraph boundaries, and the 35%
    secondary-speaker balance rule.
    """

    plan = load_json(plan_path)
    max_secondary_ratio = float(plan.get("max_secondary_ratio", 1.35))
    by_order = {int(item["order"]): item for item in sentences}
    selected_orders: set[int] = set()
    reviewed_blocks: list[dict[str, Any]] = []
    errors: list[str] = []

    for block_index, block in enumerate(plan.get("blocks", []), start=1):
        speaker = str(block.get("speaker") or "").strip()
        orders = [int(value) for value in block.get("orders", [])]
        if not speaker or not orders:
            errors.append(f"第{block_index}个语义段缺少 speaker 或 orders")
            continue
        if len(orders) != len(set(orders)):
            errors.append(f"第{block_index}个语义段包含重复句号")
            continue
        items: list[dict[str, Any]] = []
        for order in orders:
            item = by_order.get(order)
            if item is None:
                errors.append(f"第{block_index}个语义段引用不存在的句号 {order}")
                continue
            if item["speaker"] != speaker:
                errors.append(
                    f"第{block_index}个语义段的句号 {order} 属于 {item['speaker']}，不是 {speaker}"
                )
                continue
            if order in selected_orders:
                errors.append(f"句号 {order} 被多个语义段重复使用")
                continue
            items.append(item)
        if len(items) != len(orders):
            continue
        for left, right in zip(items, items[1:]):
            left_order = int(left["order"])
            right_order = int(right["order"])
            if right_order == left_order + 1:
                continue
            skipped = [
                by_order[order]
                for order in range(left_order + 1, right_order)
                if order in by_order
            ]
            if not skipped or not all(
                item["speaker"] == speaker and _noisy_context(item["text"])
                for item in skipped
            ):
                errors.append(f"第{block_index}个语义段包含无法解释的句号跳跃：{orders}")
                break
            continue
        if _dependent_start(items[0]["text"]):
            errors.append(f"第{block_index}个语义段开头依赖上文：{orders[0]}")
        if _incomplete_end(items[-1]["text"]):
            errors.append(f"第{block_index}个语义段结尾没有说完：{orders[-1]}")
        if any(_noisy_context(item["text"]) for item in items):
            errors.append(f"第{block_index}个语义段含现场口令或重录提示：{orders}")
        selected_orders.update(orders)
        reviewed_blocks.append(
            {
                "speaker": speaker,
                "orders": orders,
                "reason": block.get("reason", "AI语义审阅：保留完整表达"),
                "source_seconds": round(
                    sum(float(item["source_duration_seconds"]) for item in items), 3
                ),
            }
        )

    speakers = list(dict.fromkeys(item["speaker"] for item in sentences))
    missing_speakers = [
        speaker
        for speaker in speakers
        if not any(
            item["speaker"] == speaker and int(item["order"]) in selected_orders
            for item in sentences
        )
    ]
    if missing_speakers:
        errors.append("以下人物没有出场：" + "、".join(missing_speakers))

    selected_chars, selected_seconds = _selection_totals(sentences, selected_orders)
    if not (min_chars <= selected_chars <= max_chars):
        errors.append(f"V2字数 {selected_chars} 不在 {min_chars}-{max_chars} 范围内")
    if not (min_seconds <= selected_seconds <= max_seconds):
        errors.append(
            f"V2源素材时长 {selected_seconds:.2f} 秒不在 "
            f"{min_seconds:.0f}-{max_seconds:.0f} 秒范围内"
        )

    secondary_durations = {
        speaker: round(
            sum(
                float(item["source_duration_seconds"])
                for item in sentences
                if item["speaker"] == speaker and int(item["order"]) in selected_orders
            ),
            3,
        )
        for speaker in speakers
        if speaker != primary
    }
    positive_secondary = [value for value in secondary_durations.values() if value > 0]
    secondary_ratio = (
        max(positive_secondary) / min(positive_secondary)
        if len(positive_secondary) >= 2
        else 1.0
    )
    within_secondary_balance = secondary_ratio <= max_secondary_ratio + 1e-9
    if not within_secondary_balance:
        errors.append(
            "次要人物时长差距超过35%："
            f"最长/最短={secondary_ratio:.3f}，上限={max_secondary_ratio:.3f}"
        )
    primary_seconds = round(
        sum(
            float(item["source_duration_seconds"])
            for item in sentences
            if item["speaker"] == primary and int(item["order"]) in selected_orders
        ),
        3,
    )
    primary_is_longest = not positive_secondary or primary_seconds >= max(positive_secondary)
    if not primary_is_longest:
        errors.append(
            f"最高职位者 {primary} 的时长 {primary_seconds:.2f} 秒少于次要人物最长时长"
        )

    defects: list[dict[str, Any]] = []
    for block in reviewed_blocks:
        items = [by_order[int(order)] for order in block["orders"]]
        if _dependent_start(items[0]["text"]):
            defects.append(
                {
                    "type": "dependent_start",
                    "order": int(items[0]["order"]),
                    "speaker": items[0]["speaker"],
                    "text": items[0]["text"],
                }
            )
        if _incomplete_end(items[-1]["text"]):
            defects.append(
                {
                    "type": "incomplete_end",
                    "order": int(items[-1]["order"]),
                    "speaker": items[-1]["speaker"],
                    "text": items[-1]["text"],
                }
            )
    if defects:
        errors.append(f"仍有 {len(defects)} 个疑似语义断口")
    if errors:
        raise ValueError("V2语义审阅计划未通过硬校验：\n- " + "\n- ".join(errors))

    paragraph_by_order: dict[int, int] = {}
    for paragraph, block in enumerate(reviewed_blocks, start=1):
        for order in block["orders"]:
            paragraph_by_order[int(order)] = paragraph
    coherence_report = {
        "strategy": "AI-reviewed complete semantic blocks + hard validation",
        "original_selected_count": len(selected_orders),
        "final_selected_count": len(selected_orders),
        "context_orders_added": [],
        "single_gap_orders_bridged": [],
        "removed_runs": [],
        "paragraph_count": len(reviewed_blocks),
        "paragraph_by_order": paragraph_by_order,
        "remaining_defects": defects,
        "reviewed_blocks": reviewed_blocks,
        "requires_review": False,
    }
    balance_report = {
        "rule": "excluding_primary_max_divided_by_min_must_be_lte_1.35",
        "primary_speaker": primary,
        "primary_source_seconds": primary_seconds,
        "primary_is_longest": primary_is_longest,
        "speaker_source_seconds": secondary_durations,
        "max_min_ratio": round(secondary_ratio, 4),
        "max_allowed_ratio": max_secondary_ratio,
        "within_35_percent": within_secondary_balance,
    }
    return selected_orders, coherence_report, balance_report


def shorten_script(
    work: Path,
    primary_speaker: str | None,
    target_minutes: float,
    max_minutes: float,
    min_chars: int,
    max_chars: int,
) -> dict[str, Any]:
    corrected_v1_path = work / "script_v1_corrected.json"
    v1_path = corrected_v1_path if corrected_v1_path.exists() else work / "script_v1.json"
    if not v1_path.exists():
        raise FileNotFoundError("没有找到 script_v1.json，请先运行 build-v1。")
    v1 = load_json(v1_path)
    if v1_path == corrected_v1_path and not v1.get("ready_for_v2", False):
        raise ValueError(
            "script_v1_corrected.json 仍有需要复听的句子；"
            "请完成 v1_correction_report.md 中的复核后再生成 V2。"
        )
    speakers = list(v1["speaker_order"])
    sentences = [dict(item) for item in v1["sentences"]]
    role_scores = {
        speaker: max(
            (
                transcript_role_score(item["text"])
                for item in sentences
                if item["speaker"] == speaker
            ),
            default=0,
        )
        for speaker in speakers
    }
    if primary_speaker:
        primary = primary_speaker
        primary_selection_basis = "explicit"
    else:
        primary = max(
            speakers,
            key=lambda speaker: (role_scores[speaker], -speakers.index(speaker)),
        )
        primary_selection_basis = (
            "detected_highest_position" if role_scores[primary] > 0 else "speaker_priority"
        )
    if primary not in speakers:
        raise ValueError(f"主讲人不存在：{primary}")
    primary_sentences = [item for item in sentences if item["speaker"] == primary]
    outcome_keywords = ("成交", "定金", "提升", "压缩", "认可", "反馈", "赢单", "降低", "提高")
    target_chars = min(max_chars, max(min_chars, int(round(target_minutes * 60 * 5.0))))
    target_seconds = min(350.0, max(300.0, target_minutes * 60.0))
    min_seconds = 300.0
    # Reserve six seconds for cut padding. The ceiling can be relaxed to 6:30
    # when complete semantic paragraphs need more room.
    max_source_seconds = max(target_seconds, max_minutes * 60.0 - 6.0)
    primary_target_share = 0.40
    secondary_max_share = 0.24
    primary_target_chars = math.ceil(target_chars * primary_target_share)

    for item in sentences:
        text = item["text"]
        score = 6.0 if item["speaker"] == primary else 2.0
        if item.get("protected_data"):
            score += 12.0
        if any(keyword in text for keyword in outcome_keywords):
            score += 2.5
        topics = sentence_topics(text)
        score += min(2.0, len(topics) * 0.45)
        redundant_with_primary = False
        if item["speaker"] != primary:
            for main in primary_sentences:
                lexical = char_bigram_similarity(text, main["text"])
                shared_topics = sentence_topics(text) & sentence_topics(main["text"])
                if (
                    lexical >= 0.40
                    or len(shared_topics) >= 2
                    or (lexical >= 0.16 and len(shared_topics) >= 1)
                ):
                    redundant_with_primary = True
                    break
            if redundant_with_primary and not item.get("protected_data"):
                score -= 4.0
        item["importance_score"] = round(score, 3)
        item["redundant_with_primary"] = redundant_with_primary
        # Chinese production teams usually count visible characters in Word,
        # including punctuation but excluding spaces.  Keep that convention
        # for the 1500-1800 character target.
        item["char_count"] = len(re.sub(r"\s+", "", text))
        item["source_duration_seconds"] = round(
            max(0.0, float(item.get("source_out") or 0.0) - float(item.get("source_in") or 0.0)),
            3,
        )
        item["speech_density"] = round(
            item["char_count"] / max(0.5, item["source_duration_seconds"]),
            3,
        )
        item["data_points"] = sorted(data_fingerprints(text))
        item["specific_data_recognized"] = bool(item["data_points"])
        if item.get("protected_data") and not item["specific_data_recognized"]:
            # Bare digits and ordinal phrases such as "第二天" are not by
            # themselves a key business metric.  They may still be selected
            # for narrative value, but should not receive hard data priority.
            item["importance_score"] = round(item["importance_score"] - 12.0, 3)

    covered_data_by_speaker: dict[str, set[str]] = {speaker: set() for speaker in speakers}
    for item in sentences:
        points = set(item["data_points"])
        duplicate_data = bool(
            item.get("protected_data")
            and points
            and points.issubset(covered_data_by_speaker[item["speaker"]])
        )
        item["duplicate_data"] = duplicate_data
        item["effective_protected_data"] = bool(
            item.get("protected_data") and points and not duplicate_data
        )
        if item["effective_protected_data"]:
            covered_data_by_speaker[item["speaker"]].update(points)
        elif duplicate_data:
            item["importance_score"] = round(item["importance_score"] - 12.0, 3)

    selected_orders = {
        item["order"] for item in sentences if item.get("effective_protected_data")
    }
    # Keep at least one representative sentence from every person so a
    # multi-person benchmark does not silently collapse into a single-speaker
    # video.  This seed still obeys the whole-sentence-only rule.
    for speaker in speakers:
        if any(
            item["order"] in selected_orders and item["speaker"] == speaker
            for item in sentences
        ):
            continue
        speaker_items = [item for item in sentences if item["speaker"] == speaker]
        if speaker_items:
            representative = max(
                speaker_items,
                key=lambda item: (item["importance_score"], item["char_count"]),
            )
            selected_orders.add(representative["order"])

    # Reserve the main narrative for the highest-position speaker before
    # filling the remaining timeline.  Without this hard reservation, many
    # lower-position data sentences can consume the whole duration budget.
    primary_required_chars = sum(
        item["char_count"]
        for item in sentences
        if item["order"] in selected_orders and item["speaker"] == primary
    )
    primary_candidates = sorted(
        (
            item
            for item in sentences
            if item["speaker"] == primary and item["order"] not in selected_orders
        ),
        key=lambda item: (
            item["importance_score"] + min(6.0, item["speech_density"]),
            item["speech_density"],
            -item["order"],
        ),
        reverse=True,
    )
    for item in primary_candidates:
        if primary_required_chars >= primary_target_chars:
            break
        selected_orders.add(item["order"])
        primary_required_chars += item["char_count"]

    required_orders = set(selected_orders)
    selected_chars = sum(item["char_count"] for item in sentences if item["order"] in selected_orders)
    selected_seconds = sum(
        item["source_duration_seconds"] for item in sentences if item["order"] in selected_orders
    )
    optional = [item for item in sentences if item["order"] not in selected_orders]
    optional.sort(
        key=lambda item: (
            (item["importance_score"] + min(6.0, item["speech_density"]) * 0.8)
            / max(1.0, item["source_duration_seconds"]),
            item["speaker"] == primary,
            item["importance_score"],
        ),
        reverse=True,
    )

    # A secondary speaker may contribute unique evidence, but should not
    # become the de-facto main narrator.  Keep only that speaker's strongest
    # optional candidates up to a configurable share; protected data already
    # selected above remains untouched.
    required_chars_by_speaker = {
        speaker: sum(
            item["char_count"]
            for item in sentences
            if item["order"] in required_orders and item["speaker"] == speaker
        )
        for speaker in speakers
    }
    secondary_optional_limit = math.floor(target_chars * secondary_max_share)
    admitted_optional_chars = {speaker: 0 for speaker in speakers}
    balanced_optional: list[dict[str, Any]] = []
    for item in optional:
        speaker = item["speaker"]
        if speaker != primary:
            projected = (
                required_chars_by_speaker[speaker]
                + admitted_optional_chars[speaker]
                + item["char_count"]
            )
            if item.get("redundant_with_primary") or projected > secondary_optional_limit:
                continue
            admitted_optional_chars[speaker] += item["char_count"]
        balanced_optional.append(item)
    optional = balanced_optional
    for item in optional:
        if selected_chars >= target_chars and selected_seconds >= min_seconds:
            break
        if (
            selected_chars + item["char_count"] <= max_chars
            and selected_seconds + item["source_duration_seconds"] <= max_source_seconds
        ):
            selected_orders.add(item["order"])
            selected_chars += item["char_count"]
            selected_seconds += item["source_duration_seconds"]

    # If whole-sentence packing leaves the result short, use the shortest
    # remaining high-value sentence that still fits the hard maximum.
    if selected_chars < min_chars or selected_seconds < min_seconds:
        for item in sorted(
            optional,
            key=lambda value: (
                -(value["speech_density"]),
                value["source_duration_seconds"],
                -value["importance_score"],
            ),
        ):
            if item["order"] in selected_orders:
                continue
            if (
                selected_chars + item["char_count"] <= max_chars
                and selected_seconds + item["source_duration_seconds"] <= max_source_seconds
            ):
                selected_orders.add(item["order"])
                selected_chars += item["char_count"]
                selected_seconds += item["source_duration_seconds"]
            if selected_chars >= min_chars and selected_seconds >= min_seconds:
                break

    # A simple greedy pass can get stuck just below the character target when
    # the timeline is already nearly full.  Try one-for-one and one-for-two
    # swaps so denser sentences can replace slower, lower-value selections.
    # Protected data sentences are never candidates for removal.
    while selected_chars < min_chars:
        selected_optional = [
            item
            for item in sentences
            if item["order"] in selected_orders and not item.get("effective_protected_data")
        ]
        unselected_optional = [
            item for item in sentences if item["order"] not in selected_orders
        ]
        best_swap: tuple[tuple[float, ...], dict[str, Any], tuple[dict[str, Any], ...]] | None = None
        removal_sets: list[tuple[dict[str, Any], ...]] = [tuple()]
        removal_sets.extend((item,) for item in selected_optional)
        removal_sets.extend(combinations(selected_optional, 2))
        for added in unselected_optional:
            for removed in removal_sets:
                removed_orders = {item["order"] for item in removed}
                # Do not remove the final selected appearance of a speaker.
                if any(
                    sum(
                        1
                        for item in sentences
                        if item["speaker"] == removed_item["speaker"]
                        and item["order"] in selected_orders
                        and item["order"] not in removed_orders
                    )
                    == 0
                    for removed_item in removed
                ):
                    continue
                removed_chars = sum(item["char_count"] for item in removed)
                removed_seconds = sum(item["source_duration_seconds"] for item in removed)
                new_chars = selected_chars - removed_chars + added["char_count"]
                new_seconds = selected_seconds - removed_seconds + added["source_duration_seconds"]
                if not (selected_chars < new_chars <= max_chars):
                    continue
                if new_seconds > max_source_seconds:
                    continue
                removed_value = sum(item["importance_score"] for item in removed)
                value_change = added["importance_score"] - removed_value
                rank = (
                    1.0 if new_chars >= min_chars else 0.0,
                    float(new_chars - selected_chars),
                    value_change,
                    -abs(target_seconds - new_seconds),
                )
                if best_swap is None or rank > best_swap[0]:
                    best_swap = (rank, added, removed)
        if best_swap is None:
            break
        _, added, removed = best_swap
        for item in removed:
            selected_orders.remove(item["order"])
            selected_chars -= item["char_count"]
            selected_seconds -= item["source_duration_seconds"]
        selected_orders.add(added["order"])
        selected_chars += added["char_count"]
        selected_seconds += added["source_duration_seconds"]

    # Use a compact bitset knapsack as the final packer.  It searches for a
    # whole-sentence combination that satisfies both character count and real
    # source duration.  Optional items are ordered by editorial value, so
    # backtracking naturally prefers the primary speaker and non-redundant
    # material when several combinations are equally feasible.
    required_items = [item for item in sentences if item["order"] in required_orders]
    base_chars = sum(item["char_count"] for item in required_items)
    base_seconds = sum(item["source_duration_seconds"] for item in required_items)
    pack_items = [item for item in optional if item["order"] not in required_orders]
    bucket_seconds = 0.25
    capacity_buckets = max(
        0,
        math.floor((max_source_seconds - base_seconds) / bucket_seconds),
    )
    add_char_limit = max(0, max_chars - base_chars)
    greedy_within_target = (
        min_chars <= selected_chars <= max_chars
        and min_seconds <= selected_seconds <= max_source_seconds
    )
    if capacity_buckets and add_char_limit and not greedy_within_target:
        reach = [0] * (capacity_buckets + 1)
        reach[0] = 1
        history = [reach]
        char_mask = (1 << (add_char_limit + 1)) - 1
        item_buckets: list[int] = []
        for item in pack_items:
            duration_buckets = max(
                1,
                math.ceil(item["source_duration_seconds"] / bucket_seconds),
            )
            item_buckets.append(duration_buckets)
            updated = reach.copy()
            for used in range(capacity_buckets - duration_buckets, -1, -1):
                if reach[used]:
                    updated[used + duration_buckets] |= (
                        reach[used] << item["char_count"]
                    ) & char_mask
            reach = updated
            history.append(reach)

        min_add_chars = max(0, min_chars - base_chars)
        target_add_chars = max(0, target_chars - base_chars)
        min_duration_buckets = max(
            0,
            math.ceil((min_seconds - base_seconds) / bucket_seconds),
        )
        target_duration_buckets = max(
            0,
            round((target_seconds - base_seconds) / bucket_seconds),
        )
        packed_target: tuple[int, int] | None = None
        packed_rank: tuple[int, int] | None = None
        for used in range(min_duration_buckets, capacity_buckets + 1):
            possible = reach[used]
            if not possible:
                continue
            for chars_added in range(min_add_chars, add_char_limit + 1):
                if not ((possible >> chars_added) & 1):
                    continue
                rank = (
                    abs(chars_added - target_add_chars),
                    abs(used - target_duration_buckets),
                )
                if packed_rank is None or rank < packed_rank:
                    packed_rank = rank
                    packed_target = (used, chars_added)

        if packed_target is not None:
            used, chars_added = packed_target
            packed_orders = set(required_orders)
            for item_index in range(len(pack_items), 0, -1):
                if (history[item_index - 1][used] >> chars_added) & 1:
                    continue
                item = pack_items[item_index - 1]
                packed_orders.add(item["order"])
                used -= item_buckets[item_index - 1]
                chars_added -= item["char_count"]
            packed_items = [item for item in sentences if item["order"] in packed_orders]
            packed_seconds = sum(item["source_duration_seconds"] for item in packed_items)
            packed_chars = sum(item["char_count"] for item in packed_items)
            if (
                min_chars <= packed_chars <= max_chars
                and min_seconds <= packed_seconds <= max_source_seconds
            ):
                selected_orders = packed_orders
                selected_chars = packed_chars
                selected_seconds = packed_seconds

    hard_required_orders = {
        int(item["order"])
        for item in sentences
        if item.get("effective_protected_data")
    }
    selected_orders, coherence_report = repair_v2_coherence(
        sentences,
        selected_orders,
        hard_required_orders,
        primary,
        min_chars,
        max_chars,
        min_seconds,
        max_source_seconds,
    )
    editorial_plan_path = work / "v2_editorial_plan.json"
    if editorial_plan_path.exists():
        selected_orders, coherence_report, secondary_balance = apply_v2_editorial_plan(
            sentences,
            editorial_plan_path,
            primary,
            min_chars,
            max_chars,
            min_seconds,
            max_source_seconds,
        )
        editorial_plan_origin = str(editorial_plan_path.resolve())
    else:
        secondary_seconds = {
            speaker: round(
                sum(
                    float(item["source_duration_seconds"])
                    for item in sentences
                    if item["speaker"] == speaker
                    and int(item["order"]) in selected_orders
                ),
                3,
            )
            for speaker in speakers
            if speaker != primary
        }
        values = [value for value in secondary_seconds.values() if value > 0]
        ratio = max(values) / min(values) if len(values) >= 2 else 1.0
        secondary_balance = {
            "rule": "excluding_primary_max_divided_by_min_must_be_lte_1.35",
            "primary_speaker": primary,
            "speaker_source_seconds": secondary_seconds,
            "max_min_ratio": round(ratio, 4),
            "max_allowed_ratio": 1.35,
            "within_35_percent": ratio <= 1.35 + 1e-9,
            "requires_editorial_plan": ratio > 1.35 + 1e-9,
        }
        editorial_plan_origin = None
    selected_chars, selected_seconds = _selection_totals(sentences, selected_orders)

    output_sentences = []
    for item in sentences:
        kept = item["order"] in selected_orders
        output = dict(item)
        output["kept"] = kept
        if kept:
            output["paragraph_id"] = coherence_report["paragraph_by_order"].get(
                int(item["order"])
            )
        output_sentences.append(output)
    kept_sentences = [item for item in output_sentences if item["kept"]]
    speaker_distribution = []
    for speaker in speakers:
        speaker_items = [item for item in kept_sentences if item["speaker"] == speaker]
        speaker_chars = sum(item["char_count"] for item in speaker_items)
        speaker_seconds = sum(item["source_duration_seconds"] for item in speaker_items)
        speaker_distribution.append(
            {
                "speaker": speaker,
                "role_score": role_scores[speaker],
                "sentences": len(speaker_items),
                "chars": speaker_chars,
                "char_share": round(speaker_chars / max(1, selected_chars), 4),
                "source_seconds": round(speaker_seconds, 3),
                "time_share": round(speaker_seconds / max(0.001, selected_seconds), 4),
            }
        )
    result = {
        "schema_version": 2,
        "source_v1": str(v1_path.resolve()),
        "primary_speaker": primary,
        "primary_selection_basis": primary_selection_basis,
        "speaker_role_scores": role_scores,
        "primary_target_share": primary_target_share,
        "secondary_max_share": secondary_max_share,
        "speaker_distribution": speaker_distribution,
        "target_minutes": target_minutes,
        "max_minutes": max_minutes,
        "target_chars": target_chars,
        "min_chars": min_chars,
        "max_chars": max_chars,
        "selected_chars": selected_chars,
        "estimated_minutes": round(selected_chars / 5.0 / 60, 2),
        "selected_source_seconds": round(selected_seconds, 3),
        "selected_source_minutes": round(selected_seconds / 60.0, 2),
        "duration_target_seconds": target_seconds,
        "within_character_range": min_chars <= selected_chars <= max_chars,
        "within_duration_range": min_seconds <= selected_seconds <= max_source_seconds,
        "within_target_range": (
            min_chars <= selected_chars <= max_chars
            and min_seconds <= selected_seconds <= max_source_seconds
            and secondary_balance["within_35_percent"]
        ),
        "coherence": coherence_report,
        "editorial_plan": editorial_plan_origin,
        "secondary_balance": secondary_balance,
        "sentences": kept_sentences,
        "decisions": output_sentences,
    }
    write_json(work / "script_v2.json", result)
    markdown = ["# 文稿 V2", ""]
    grouped_paragraphs: list[tuple[str, int | None, list[str]]] = []
    for item in kept_sentences:
        key = (item["speaker"], item.get("paragraph_id"))
        if grouped_paragraphs and grouped_paragraphs[-1][:2] == key:
            grouped_paragraphs[-1][2].append(item["text"])
        else:
            grouped_paragraphs.append((key[0], key[1], [item["text"]]))
    current_speaker = None
    for speaker, _, texts in grouped_paragraphs:
        if speaker != current_speaker:
            current_speaker = speaker
            markdown.extend([f"## {speaker}", ""])
        normalized = []
        for index, text in enumerate(texts):
            compact = text.strip()
            if index + 1 < len(texts):
                compact = compact.rstrip("。！？!?；;") + "，"
            normalized.append(compact)
        markdown.extend(["".join(normalized), ""])
    (work / "script_v2.md").write_text("\n".join(markdown), encoding="utf-8")
    coherence_lines = [
        "# V2 连贯性检查",
        "",
        f"- 连续语义段：{coherence_report['paragraph_count']}",
        f"- 补入上下文句段：{len(coherence_report['context_orders_added'])}",
        f"- 补齐单句空洞：{len(coherence_report['single_gap_orders_bridged'])}",
        f"- 整段删除：{len(coherence_report['removed_runs'])}",
        f"- 剩余疑似断句：{len(coherence_report['remaining_defects'])}",
        f"- 次要人物最长/最短时长比：{secondary_balance['max_min_ratio']:.3f}（上限 1.350）",
        f"- 次要人物时长差距不超过35%：{'是' if secondary_balance['within_35_percent'] else '否'}",
        "",
    ]
    for defect in coherence_report["remaining_defects"]:
        coherence_lines.append(
            f"- 需复核：{defect['speaker']} 第{defect['order']}句 "
            f"({defect['type']})：{defect['text']}"
        )
    (work / "script_v2_coherence_report.md").write_text(
        "\n".join(coherence_lines), encoding="utf-8"
    )
    return result


def align_script(project: Path, work: Path, accept_score: float, review_score: float, padding: float) -> dict[str, Any]:
    inventory = ensure_inventory(project, work)
    transcript_path = work / "transcript.json"
    if not transcript_path.exists():
        raise FileNotFoundError("没有找到 transcript.json，请先运行 transcribe。")
    transcript = load_json(transcript_path)
    script_v2_path = work / "script_v2.json"
    if script_v2_path.exists():
        script_v2 = load_json(script_v2_path)
        structured_sections = [dict(item) for item in script_v2["sentences"]]
    else:
        if not inventory.get("script"):
            raise FileNotFoundError(
                "没有 script_v2.json，也没有可用讲稿。请先运行 build-v1 和 shorten-script。"
            )
        structured_sections = script_sections(Path(inventory["script"]))
    direct_anchors = all(
        section.get("source")
        and section.get("source_in") is not None
        and section.get("source_out") is not None
        for section in structured_sections
    )
    if direct_anchors:
        selections = [None] * len(structured_sections)
        speaker_margins = [1.0] * len(structured_sections)
        source_speakers: dict[str, dict[str, Any]] = {}
        for section in structured_sections:
            source_speakers[str(section["source"])] = {
                "speaker": section["speaker"],
                "score": 1.0,
                "margin": 1.0,
            }
    else:
        selections, speaker_margins, source_speakers = select_content_matches(
            structured_sections, transcript
        )
    media_durations = {
        str(Path(item["path"]).resolve()).lower(): float(item.get("duration_seconds") or 0.0)
        for item in inventory.get("a_roll", [])
    }
    rows = []
    for index, (script_section, candidate, speaker_margin) in enumerate(
        zip(structured_sections, selections, speaker_margins), start=1
    ):
        paragraph = script_section["text"]
        speaker = script_section["speaker"]
        if (
            script_section.get("source")
            and script_section.get("source_in") is not None
            and script_section.get("source_out") is not None
        ):
            source = str(script_section["source"])
            source_duration = media_durations.get(str(Path(source).resolve()).lower(), 0.0)
            source_out = float(script_section["source_out"]) + padding
            if source_duration > 0:
                source_out = min(source_duration, source_out)
            rows.append(
                {
                    "section": index,
                    "speaker": speaker,
                    "script": paragraph,
                    "status": "matched",
                    "score": 1.0,
                    "speaker_margin": 1.0,
                    "speaker_status": "matched",
                    "source": source,
                    "source_in": round(max(0.0, float(script_section["source_in"]) - padding), 3),
                    "source_out": round(source_out, 3),
                    "transcript": paragraph,
                    "anchor_origin": "v1_transcript_timecode",
                }
            )
            continue
        if candidate is None:
            rows.append(
                {
                    "section": index,
                    "speaker": speaker,
                    "script": paragraph,
                    "status": "missing",
                    "score": 0.0,
                    "speaker_margin": 0.0,
                    "speaker_status": "unknown",
                    "source": None,
                    "source_in": None,
                    "source_out": None,
                    "transcript": "",
                    "anchor_origin": "fuzzy_match",
                }
            )
            continue
        status = (
            "matched"
            if candidate.score >= accept_score and speaker_margin >= 0.08
            else "review"
            if candidate.score >= review_score
            else "missing"
        )
        speaker_status = (
            "matched"
            if candidate.score >= 0.35 and speaker_margin >= 0.12
            else "review"
            if candidate.score >= 0.30 and speaker_margin >= 0.05
            else "unknown"
        )
        rows.append(
            {
                "section": index,
                "speaker": speaker,
                "script": paragraph,
                "status": status,
                "score": round(candidate.score, 4),
                "speaker_margin": speaker_margin,
                "speaker_status": speaker_status,
                "source": candidate.source,
                "source_in": round(max(0.0, candidate.start - padding), 3),
                "source_out": round(candidate.end + padding, 3),
                "transcript": candidate.text,
                "anchor_origin": "fuzzy_match",
            }
        )

    # Padding should not create overlaps between adjacent selections from the same source.
    for previous, current in zip(rows, rows[1:]):
        if (
            previous["source"]
            and previous["source"] == current["source"]
            and previous["source_out"] is not None
            and current["source_in"] is not None
            and previous["source_out"] > current["source_in"]
        ):
            boundary = round((previous["source_out"] + current["source_in"]) / 2, 3)
            previous["source_out"] = boundary
            current["source_in"] = boundary

    plan = {
        "schema_version": 2,
        "project_root": str(project.resolve()),
        "accept_score": accept_score,
        "review_score": review_score,
        "speakers": list(dict.fromkeys(section["speaker"] for section in structured_sections)),
        "matching_strategy": (
            "direct V1 transcript timecodes; no second fuzzy match"
            if direct_anchors
            else "global transcript-to-script content matching; no voice or face recognition"
        ),
        "source_speaker_assignments": source_speakers,
        "sections": rows,
    }
    write_json(work / "roughcut_plan.json", plan)
    with (work / "roughcut_plan.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    uncertain = [row for row in rows if row["status"] != "matched"]
    report_lines = [
        "# A-roll 粗剪审核报告",
        "",
        f"- 讲稿段落：{len(rows)}",
        f"- 识别人物：{', '.join(plan['speakers'])}",
        f"- 自动通过：{len(rows) - len(uncertain)}",
        f"- 需要复核或缺失：{len(uncertain)}",
        f"- 人物自动确认：{sum(row['speaker_status'] == 'matched' for row in rows)}",
        f"- 人物需复核或未知：{sum(row['speaker_status'] != 'matched' for row in rows)}",
        "",
        "## 需要人工检查",
        "",
    ]
    if not uncertain:
        report_lines.append("所有段落均达到自动通过阈值。")
    else:
        for row in uncertain:
            report_lines.extend(
                [
                    f"### 第 {row['section']} 段：{row['status']}（{row['score']:.2f}）",
                    "",
                    f"- 人物：{row['speaker']}；人物状态 {row['speaker_status']}（与其他人物的匹配分差 {row['speaker_margin']:.2f}）",
                    f"- 讲稿：{row['script']}",
                    f"- 素材：{row['source'] or '未找到'}",
                    f"- 时间：{row['source_in']} - {row['source_out']}",
                    f"- 识别文本：{row['transcript'] or '无'}",
                    "",
                ]
            )
    (work / "review_report.md").write_text("\n".join(report_lines), encoding="utf-8")
    return plan


def concat_quote(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")


def render_preview(project: Path, work: Path, min_score: float, crf: int) -> Path:
    plan_path = work / "roughcut_plan.json"
    if not plan_path.exists():
        raise FileNotFoundError("没有找到 roughcut_plan.json，请先运行 align。")
    plan = load_json(plan_path)
    ffmpeg = find_ffmpeg()
    temp_dir = work / "preview_segments"
    temp_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for row in plan["sections"]:
        if row["status"] == "missing" or float(row["score"]) < min_score:
            continue
        target = temp_dir / f"section_{int(row['section']):03d}.mp4"
        duration = max(0.05, float(row["source_out"]) - float(row["source_in"]))
        run_process(
            [
                ffmpeg,
                "-y",
                "-ss",
                f"{float(row['source_in']):.3f}",
                "-i",
                row["source"],
                "-t",
                f"{duration:.3f}",
                "-vf",
                "scale=-2:720",
                "-r",
                "25",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                str(crf),
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                str(target),
            ]
        )
        outputs.append(target)
    if not outputs:
        raise RuntimeError("没有达到预览阈值的片段。请先检查 roughcut_plan.json。")
    manifest = temp_dir / "concat.txt"
    manifest.write_text("\n".join(f"file '{concat_quote(path)}'" for path in outputs), encoding="utf-8")
    target = work / "preview_aroll.mp4"
    run_process([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(manifest), "-c", "copy", str(target)])
    return target


def create_contact_sheets(work: Path, columns: int, rows_per_sheet: int) -> list[Path]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("缺少 Pillow。请先运行 scripts/setup.ps1。") from exc

    index_path = work / "broll_index.json"
    if not index_path.exists():
        raise FileNotFoundError("没有找到 broll_index.json，请先运行 thumbnails。")
    clips = load_json(index_path)["clips"]
    cell_w, image_h, label_h = 640, 360, 44
    sheet_dir = work / "broll_contact_sheets"
    sheet_dir.mkdir(parents=True, exist_ok=True)
    font_path = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts" / "msyh.ttc"
    font = ImageFont.truetype(str(font_path), 24) if font_path.exists() else ImageFont.load_default()
    batch_size = columns * rows_per_sheet
    outputs = []
    for batch_index in range(0, len(clips), batch_size):
        batch = clips[batch_index : batch_index + batch_size]
        actual_rows = (len(batch) + columns - 1) // columns
        canvas = Image.new("RGB", (columns * cell_w, actual_rows * (image_h + label_h)), "white")
        draw = ImageDraw.Draw(canvas)
        for index, clip in enumerate(batch):
            row, column = divmod(index, columns)
            x, y = column * cell_w, row * (image_h + label_h)
            with Image.open(clip["thumbnail"]) as source:
                image = source.convert("RGB")
                image.thumbnail((cell_w, image_h))
                frame = Image.new("RGB", (cell_w, image_h), "#111111")
                frame.paste(image, ((cell_w - image.width) // 2, (image_h - image.height) // 2))
                canvas.paste(frame, (x, y))
            label = Path(clip["source"]).name
            draw.rectangle((x, y + image_h, x + cell_w, y + image_h + label_h), fill="#F4F6F8")
            draw.text((x + 14, y + image_h + 7), label, font=font, fill="#172033")
        target = sheet_dir / f"broll_sheet_{batch_index // batch_size + 1:02d}.jpg"
        canvas.save(target, quality=90)
        outputs.append(target)
    return outputs


def expand_name_range(start: int, end: int) -> list[str]:
    return [f"C{number}.MP4" for number in range(start, end + 1)]


def apply_broll_labels(work: Path, labels_path: Path) -> dict[str, Any]:
    index_path = work / "broll_index.json"
    if not index_path.exists():
        raise FileNotFoundError("没有找到 broll_index.json，请先运行 thumbnails。")
    index = load_json(index_path)
    labels = load_json(labels_path)
    by_name: dict[str, dict[str, Any]] = {}
    for group in labels.get("groups", []):
        names = list(group.get("files", []))
        if "range" in group:
            names.extend(expand_name_range(int(group["range"][0]), int(group["range"][1])))
        for name in names:
            by_name[name.lower()] = {
                "description": group["description"],
                "tags": group.get("tags", []),
                "needs_review": bool(group.get("needs_review", False)),
            }
    updated = 0
    for clip in index["clips"]:
        label = by_name.get(Path(clip["source"]).name.lower())
        if label:
            clip.update(label)
            updated += 1
    write_json(index_path, index)
    return {"updated": updated, "total": len(index["clips"]), "index": str(index_path)}


def keyword_tag_weights(script: str) -> dict[str, int]:
    rules = {
        "成立": (8, {"company_logo", "company_exterior", "company_awards"}),
        "公司": (2, {"company_logo", "company_exterior", "company_awards", "office"}),
        "客户": (2, {"customer_consultation", "customer_visit", "showroom"}),
        "未来的家": (6, {"ai_rendering", "rendering_display", "showroom_interior"}),
        "效果图": (5, {"ai_rendering", "rendering_display", "designer_ai_rendering"}),
        "预算": (9, {"floor_plan", "designer_work"}),
        "增项": (9, {"floor_plan", "customer_consultation"}),
        "沟通": (6, {"customer_consultation", "floor_plan"}),
        "需求": (6, {"customer_consultation", "floor_plan", "designer_work"}),
        "方案": (7, {"floor_plan", "ai_rendering", "rendering_display"}),
        "落地": (9, {"construction_guidance", "floor_plan"}),
        "施工": (14, {"construction_guidance", "material_showroom"}),
        "工地": (16, {"construction_guidance", "material_showroom"}),
        "放样": (16, {"construction_guidance", "material_showroom"}),
        "培训": (10, {"staff_training"}),
        "三分钟": (9, {"ai_design_platform", "ai_rendering", "designer_ai_rendering"}),
        "生成": (5, {"ai_design_platform", "ai_rendering", "designer_ai_rendering"}),
        "设计师": (8, {"designer_work", "designer_ai_rendering"}),
        "风格": (6, {"material_showroom", "designer_ai_rendering"}),
        "PPT": (10, {"customer_consultation", "ai_design_platform"}),
        "谈单": (12, {"customer_consultation", "ai_design_platform"}),
        "全屋": (7, {"ai_rendering", "showroom_interior"}),
        "组织": (8, {"staff_training", "office"}),
        "全员": (8, {"staff_training", "office"}),
        "进店": (8, {"customer_visit", "showroom", "customer_consultation"}),
        "认真": (8, {"company_awards", "company_logo"}),
        "评价": (7, {"company_awards", "company_logo"}),
        "体验": (7, {"customer_consultation", "customer_visit", "showroom"}),
        "酷家乐": (3, {"ai_design_platform", "ai_rendering", "designer_ai_rendering"}),
        "AI": (3, {"ai_design_platform", "ai_rendering", "designer_ai_rendering"}),
        "户型图": (9, {"floor_plan", "ai_design_platform"}),
        "导入": (7, {"floor_plan", "ai_design_platform"}),
        "风格": (8, {"ai_rendering", "designer_ai_rendering"}),
        "微调": (9, {"designer_ai_rendering", "ai_design_platform"}),
        "调整": (6, {"designer_ai_rendering", "ai_design_platform"}),
        "漫游": (10, {"rendering_display", "showroom_interior"}),
        "全景": (10, {"rendering_display", "showroom_interior"}),
        "增强": (7, {"ai_rendering", "designer_ai_rendering"}),
    }
    weights: dict[str, int] = {}
    for keyword, (weight, tags) in rules.items():
        if keyword in script:
            for tag in tags:
                weights[tag] = weights.get(tag, 0) + weight
    # When a sentence names a concrete action, prefer that evidence over a
    # broadly related location or atmosphere tag.
    if any(keyword in script for keyword in ("工地", "施工", "放样")):
        weights["construction_guidance"] = weights.get("construction_guidance", 0) + 10
    if any(keyword in script for keyword in ("预算", "增项")):
        weights["floor_plan"] = weights.get("floor_plan", 0) + 6
    if any(keyword in script for keyword in ("PPT", "谈单")):
        weights["customer_consultation"] = weights.get("customer_consultation", 0) + 8
    if "培训" in script:
        weights["staff_training"] = weights.get("staff_training", 0) + 8
    return weights


def split_visual_clauses(script: str) -> list[dict[str, Any]]:
    parts = [
        part.strip()
        for part in re.findall(r"[^，。！？；：,]+[，。！？；：,]?", script)
        if part.strip()
    ]
    # ASR output often has few punctuation marks.  Split long transcript
    # chunks into visual beats so a 12–15 second spoken sentence can receive
    # two or three semantically matched shots instead of only one.
    expanded: list[str] = []
    for part in parts:
        remaining = part
        while len(re.sub(r"[，。！？；：,\s]", "", remaining)) > 30:
            visible = 0
            cut = 0
            for index, character in enumerate(remaining):
                if not re.match(r"[，。！？；：,\s]", character):
                    visible += 1
                cut = index + 1
                if visible >= 24:
                    break
            expanded.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip()
        if remaining:
            expanded.append(remaining)
    parts = expanded
    merged: list[str] = []
    for part in parts:
        clean_length = len(re.sub(r"[，。！？；：\s]", "", part))
        if merged and clean_length < 5:
            merged[-1] += part
        else:
            merged.append(part)
    weights = [max(1, len(re.sub(r"[，。！？；：\s]", "", part))) for part in merged]
    total = max(1, sum(weights))
    cursor = 0
    clauses = []
    for index, (part, weight) in enumerate(zip(merged, weights)):
        start_fraction = cursor / total
        cursor += weight
        clauses.append(
            {
                "index": index,
                "text": part,
                "start_fraction": start_fraction,
                "end_fraction": cursor / total,
                "desired_tags": keyword_tag_weights(part),
            }
        )
    return clauses


def choose_quality_window(
    clip: dict[str, Any],
    analysis: dict[str, Any] | None,
    target_duration: float,
    media_duration: float,
) -> dict[str, float]:
    if analysis and analysis.get("best_windows"):
        return min(
            analysis["best_windows"],
            key=lambda item: abs(float(item["requested_duration"]) - target_duration),
        )
    if media_duration <= 1.2:
        return {
            "source_in": 0.0,
            "source_out": media_duration,
            "duration": media_duration,
            "quality_score": 40.0,
        }
    guard = min(0.8, max(0.2, (media_duration - 1.2) / 2))
    duration = min(target_duration, max(1.2, media_duration - 2 * guard))
    start = guard + max(0.0, (media_duration - 2 * guard - duration) * 0.45)
    return {
        "source_in": round(start, 3),
        "source_out": round(min(media_duration - guard, start + duration), 3),
        "duration": round(duration, 3),
        "quality_score": 40.0,
    }


def match_broll(
    work: Path,
    clips_per_section: int,
    clip_seconds: float,
    max_source_uses: int,
) -> dict[str, Any]:
    if max_source_uses < 1:
        raise ValueError("每条 B-roll 素材的使用上限必须至少为 1。")
    plan_path = work / "roughcut_plan.json"
    index_path = work / "broll_index.json"
    inventory_path = work / "media_inventory.json"
    for path in (plan_path, index_path, inventory_path):
        if not path.exists():
            raise FileNotFoundError(f"缺少 {path.name}。")
    plan = load_json(plan_path)
    index = load_json(index_path)
    inventory = load_json(inventory_path)
    durations = {media_source_key(item["path"]): float(item.get("duration_seconds") or 0) for item in inventory["b_roll"]}
    current_sources = {
        media_source_key(item["path"]): item["path"] for item in inventory["b_roll"]
    }
    analysis_path = work / "broll_analysis.json"
    analysis_by_name: dict[str, dict[str, Any]] = {}
    if analysis_path.exists():
        analysis_data = load_json(analysis_path)
        analysis_by_name = {
            media_source_key(item["source"]): item for item in analysis_data.get("clips", [])
        }
    used_counts: dict[str, int] = {}
    last_family = ""
    matches = []
    for section in plan["sections"]:
        if section.get("status") == "missing":
            matches.append(
                {
                    "section": section["section"],
                    "script": section["script"],
                    "candidates": [],
                    "unmatched_visual_clauses": [],
                }
            )
            continue
        section_duration = max(
            0.0,
            float(section.get("source_out") or 0) - float(section.get("source_in") or 0),
        )
        clauses = split_visual_clauses(section["script"])
        visual_clauses = [clause for clause in clauses if clause["desired_tags"]]
        # The two reference edits use noticeably denser B-roll than the V2
        # draft.  Roughly one visual beat per 5.5 seconds gives long sections
        # enough candidates without forcing B-roll over every spoken sentence.
        max_choices = min(clips_per_section, max(1, math.ceil(section_duration / 5.5)))
        selected_clauses = sorted(
            sorted(
                visual_clauses,
                key=lambda clause: (
                    -max(clause["desired_tags"].values()),
                    -sum(clause["desired_tags"].values()),
                    clause["index"],
                ),
            )[:max_choices],
            key=lambda clause: clause["index"],
        )
        choices = []
        used_in_section: set[str] = set()
        for clause in selected_clauses:
            clause_duration = section_duration * (
                float(clause["end_fraction"]) - float(clause["start_fraction"])
            )
            # Reference median lengths are 3.20s and 3.94s.  Use 2.8s as the
            # lower preference for ordinary shots and let concrete/longer
            # clauses reach 4.5s.
            target_duration = max(2.8, min(4.5, clause_duration * 1.05, clip_seconds))
            ranked = []
            for clip in index["clips"]:
                name = media_source_key(clip["source"])
                if clip.get("needs_review") or not clip.get("tags"):
                    continue
                # Repetition is more distracting than a lower B-roll coverage
                # ratio.  Once a source file reaches the hard cap, do not rank
                # it again even if it would otherwise be the best match.
                source_limit = broll_source_use_limit(clip, max_source_uses)
                if used_counts.get(name, 0) >= source_limit:
                    continue
                overlap = set(clause["desired_tags"]).intersection(clip["tags"])
                if not overlap:
                    continue
                semantic_score = float(sum(clause["desired_tags"][tag] for tag in overlap))
                window = choose_quality_window(
                    clip,
                    analysis_by_name.get(name),
                    target_duration,
                    durations.get(name, 0.0),
                )
                quality_score = float(window.get("quality_score", 40.0))
                family = str(clip.get("description") or "")
                reuse_penalty = used_counts.get(name, 0) * 12.0
                if name in used_in_section:
                    reuse_penalty += 100.0
                family_penalty = 12.0 if family and family == last_family else 0.0
                common_priority_bonus = broll_origin_priority_bonus(clip)
                decision_score = semantic_score * 5.0 + quality_score * 0.35 + common_priority_bonus - reuse_penalty - family_penalty
                ranked.append(
                    (
                        decision_score,
                        semantic_score,
                        quality_score,
                        name,
                        clip,
                        sorted(overlap),
                        window,
                        family,
                    )
                )
            ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
            if not ranked:
                continue
            best = ranked[0]
            margin = best[0] - ranked[1][0] if len(ranked) > 1 else best[0]
            _, semantic_score, quality_score, name, clip, reasons, window, family = best
            confidence = "high" if semantic_score >= 8 and quality_score >= 60 and margin >= 5 else "medium"
            choices.append(
                {
                    "source": current_sources.get(name, clip["source"]),
                    "source_in": round(float(window["source_in"]), 3),
                    "source_out": round(float(window["source_out"]), 3),
                    "thumbnail": clip["thumbnail"],
                    "storyboard": analysis_by_name.get(name, {}).get("storyboard"),
                    "description": clip["description"],
                    "origin": clip.get("origin", "project"),
                    "max_source_uses": source_limit,
                    "clause": clause["text"],
                    "clause_start_fraction": round(float(clause["start_fraction"]), 5),
                    "clause_end_fraction": round(float(clause["end_fraction"]), 5),
                    "matched_tags": reasons,
                    "semantic_score": round(semantic_score, 2),
                    "quality_score": round(quality_score, 2),
                    "decision_score": round(float(best[0]), 2),
                    "confidence": confidence,
                }
            )
            used_counts[name] = used_counts.get(name, 0) + 1
            used_in_section.add(name)
            last_family = family
        matches.append(
            {
                "section": section["section"],
                "script": section["script"],
                "candidates": choices,
                "unmatched_visual_clauses": [
                    clause["text"] for clause in selected_clauses if clause["text"] not in {c["clause"] for c in choices}
                ],
            }
        )
    result = {
        "schema_version": 3,
        "strategy": "dense clause semantic score + multi-frame quality score + hard source reuse cap",
        "clip_seconds_max": clip_seconds,
        "max_source_uses": max_source_uses,
        "common_ai_max_source_uses": 1,
        "source_use_counts": {
            current_sources.get(name, name): count
            for name, count in sorted(used_counts.items())
        },
        "sources_at_limit": [
            current_sources.get(name, name)
            for name, count in sorted(used_counts.items())
            if count >= (1 if any(
                media_source_key(clip["source"]) == name and clip.get("origin") == "skill_common_ai"
                for clip in index["clips"]
            ) else max_source_uses)
        ],
        "sections": matches,
    }
    write_json(work / "broll_matches.json", result)
    with (work / "broll_matches.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "section",
                "clause",
                "source",
                "source_in",
                "source_out",
                "matched_tags",
                "semantic_score",
                "quality_score",
                    "decision_score",
                "origin",
                "max_source_uses",
                "confidence",
            ],
        )
        writer.writeheader()
        for section in matches:
            for candidate in section["candidates"]:
                writer.writerow(
                    {
                        "section": section["section"],
                        "clause": candidate["clause"],
                        "source": candidate["source"],
                        "source_in": candidate["source_in"],
                        "source_out": candidate["source_out"],
                        "matched_tags": ",".join(candidate["matched_tags"]),
                        "semantic_score": candidate["semantic_score"],
                        "quality_score": candidate["quality_score"],
                        "decision_score": candidate["decision_score"],
                        "origin": candidate.get("origin", "project"),
                        "max_source_uses": candidate.get("max_source_uses", max_source_uses),
                        "confidence": candidate["confidence"],
                    }
                )
    review_lines = [
        "# B-roll 选择审核",
        "",
        "每个选择都同时记录对应口播句子、素材时间码、语义分和画面质量分。",
        f"每条源素材最多使用 {max_source_uses} 次；达到上限后宁可保留 A-roll，也不继续重复。",
        "",
    ]
    for section in matches:
        review_lines.extend([f"## 第 {section['section']} 段", ""])
        for candidate in section["candidates"]:
            review_lines.extend(
                [
                    f"- 口播：{candidate['clause']}",
                    f"- 素材：{Path(candidate['source']).name}，{candidate['source_in']:.2f}s–{candidate['source_out']:.2f}s",
                    f"- 理由：{candidate['description']}；标签 {', '.join(candidate['matched_tags'])}",
                    f"- 来源：{'Skill 通用 AI 录屏库' if candidate.get('origin') == 'skill_common_ai' else '本案例素材'}",
                    f"- 本素材全片使用上限：{candidate.get('max_source_uses', max_source_uses)} 次",
                    f"- 分数：语义 {candidate['semantic_score']:.1f} / 画面质量 {candidate['quality_score']:.1f} / 综合 {candidate['decision_score']:.1f}",
                    f"- 故事板：{candidate.get('storyboard') or '未生成'}",
                    "",
                ]
            )
        if section["unmatched_visual_clauses"]:
            review_lines.append(
                "- 未覆盖口播：" + "；".join(section["unmatched_visual_clauses"])
            )
            review_lines.append("")
    (work / "broll_review.md").write_text("\n".join(review_lines), encoding="utf-8")
    return result


def timeline_coverage_metrics(
    clips: list[dict[str, Any]],
    timeline_duration: float,
    short_gap_seconds: float,
) -> dict[str, Any]:
    intervals = sorted(
        (
            max(0.0, float(clip["timeline_in"])),
            min(timeline_duration, float(clip["timeline_out"])),
        )
        for clip in clips
        if float(clip["timeline_out"]) > float(clip["timeline_in"])
    )
    merged: list[list[float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    covered = sum(end - start for start, end in merged)
    gaps = [
        next_start - end
        for (_, end), (next_start, _) in zip(merged, merged[1:])
        if next_start > end
    ]
    visible = max(0.0, timeline_duration - covered)
    return {
        "covered_seconds": round(covered, 3),
        "coverage_ratio": round(covered / timeline_duration, 4) if timeline_duration else 0.0,
        "a_roll_visible_seconds": round(visible, 3),
        "a_roll_visible_ratio": round(visible / timeline_duration, 4) if timeline_duration else 0.0,
        "b_roll_blocks": len(merged),
        "interior_gaps": len(gaps),
        "short_gaps": sum(gap <= short_gap_seconds + 1e-6 for gap in gaps),
        "short_gap_seconds": round(sum(gap for gap in gaps if gap <= short_gap_seconds + 1e-6), 3),
    }


def close_residual_broll_flashes(clips: list[dict[str, Any]], max_gap_seconds: float = 1.5) -> int:
    """Shift an entire following contiguous block; never reopen its inner joins."""
    clips.sort(key=lambda c: (c['timeline_in'],c['timeline_out']))
    fixed=0
    for i in range(1,len(clips)):
        gap=clips[i]['timeline_in']-clips[i-1]['timeline_out']
        if not 0.001 < gap <= max_gap_seconds:
            continue
        end=i
        while end+1<len(clips) and abs(clips[end+1]['timeline_in']-clips[end]['timeline_out'])<=.002:
            end+=1
        for c in clips[i:end+1]:
            c['timeline_in']=round(c['timeline_in']-gap,4)
            c['timeline_out']=round(c['timeline_out']-gap,4)
            c['bridge_mode']='reposition_complete_stable_block'
        fixed+=1
    return fixed


def bridge_short_broll_gaps(
    clips: list[dict[str, Any]],
    media_durations: dict[str, float],
    max_gap_seconds: float,
    source_edge_guard: float = 0.65,
) -> int:
    """Extend neighbouring stable source windows to remove brief A-roll flashes."""
    if max_gap_seconds <= 0:
        return 0
    clips.sort(key=lambda item: (float(item["timeline_in"]), float(item["timeline_out"])))
    bridged = 0
    for previous, following in zip(clips, clips[1:]):
        gap = float(following["timeline_in"]) - float(previous["timeline_out"])
        if gap <= 1e-6 or gap > max_gap_seconds + 1e-6:
            continue
        previous_duration = media_durations.get(str(previous["source"]).lower(), 0.0)
        following_duration = media_durations.get(str(following["source"]).lower(), 0.0)
        previous_room = max(
            0.0,
            previous_duration - source_edge_guard - float(previous["source_out"]),
        )
        following_room = max(0.0, float(following["source_in"]) - source_edge_guard)
        if previous_room + following_room + 1e-6 < gap:
            # Keep the proven stable source windows intact.  Reposition both
            # shots by at most half the short gap instead of extending into a
            # potentially shaky recording edge.
            previous_shift = gap / 2
            following_shift = gap - previous_shift
            previous["timeline_in"] = round(float(previous["timeline_in"]) + previous_shift, 3)
            previous["timeline_out"] = round(float(previous["timeline_out"]) + previous_shift, 3)
            following["timeline_in"] = round(float(following["timeline_in"]) - following_shift, 3)
            following["timeline_out"] = round(float(following["timeline_out"]) - following_shift, 3)
            previous["bridge_to_next"] = True
            previous["bridge_mode"] = "reposition_stable_windows"
            following["bridge_from_previous"] = True
            following["bridge_mode"] = "reposition_stable_windows"
            bridged += 1
            continue

        # Split the added time between the two shots when possible so neither
        # source window is pushed unnecessarily close to a shaky recording edge.
        previous_add = min(previous_room, gap / 2)
        following_add = min(following_room, gap - previous_add)
        remainder = gap - previous_add - following_add
        if remainder > 1e-6:
            extra_previous = min(previous_room - previous_add, remainder)
            previous_add += extra_previous
            remainder -= extra_previous
        if remainder > 1e-6:
            following_add += min(following_room - following_add, remainder)

        previous["source_out"] = round(float(previous["source_out"]) + previous_add, 3)
        previous["timeline_out"] = round(float(previous["timeline_out"]) + previous_add, 3)
        following["source_in"] = round(float(following["source_in"]) - following_add, 3)
        following["timeline_in"] = round(float(following["timeline_in"]) - following_add, 3)
        previous["bridge_to_next"] = True
        previous["bridge_mode"] = "extend_stable_windows"
        following["bridge_from_previous"] = True
        following["bridge_mode"] = "extend_stable_windows"
        bridged += 1
    # A later pair-wise reposition may reopen an earlier join. Finalize whole
    # blocks once; source windows and usage counts stay unchanged.
    return bridged + close_residual_broll_flashes(clips,max_gap_seconds)


def ensure_common_ai_broll_quota(
    plan: dict[str, Any],
    inventory: dict[str, Any],
    minimum: int = 3,
    maximum: int = 5,
    target: int = 4,
) -> dict[str, Any]:
    """Guarantee a small, non-repeating set of filename-labelled AI recordings."""
    project = Path(inventory.get("project_root") or ".")
    script_value = inventory.get("script")
    script = Path(script_value) if script_value else None
    case_type = inventory.get("case_type") or detect_project_case_type(project, script)
    if case_type != "ai":
        return {"applied": False, "case_type": case_type, "used": 0, "target": 0}
    if not 0 <= minimum <= target <= maximum:
        raise ValueError("通用录屏数量必须满足 0 <= minimum <= target <= maximum。")

    common_media = {
        media_source_key(item["path"]): item
        for item in inventory.get("b_roll", [])
        if item.get("origin") == "skill_common_ai"
    }
    if len(common_media) < minimum:
        raise ValueError(f"AI案例至少需要 {minimum} 条通用录屏，但素材库只有 {len(common_media)} 条。")

    broll = sorted(
        plan["tracks"].get("b_roll", []),
        key=lambda item: (float(item["timeline_in"]), float(item["timeline_out"])),
    )
    # Enforce one use per common source and no more than the requested maximum.
    kept = []
    seen_common: set[str] = set()
    removed = 0
    for clip in broll:
        key = media_source_key(clip["source"])
        if key in common_media:
            if key in seen_common or len(seen_common) >= maximum:
                removed += 1
                continue
            seen_common.add(key)
            clip["origin"] = "skill_common_ai"
            clip["max_source_uses"] = 1
        kept.append(clip)
    broll = kept

    subtitle_by_section = {
        item.get("section"): str(item.get("text") or "")
        for item in plan["tracks"].get("subtitles", [])
    }
    replacement_indices = [
        index for index, clip in enumerate(broll)
        if media_source_key(clip["source"]) not in common_media
    ]
    unused_common = [item for key, item in common_media.items() if key not in seen_common]
    pair_candidates = []
    for media in unused_common:
        tags = set(media.get("tags") or [])
        media_duration = float(media.get("duration_seconds") or 0)
        for index in replacement_indices:
            clip = broll[index]
            text = subtitle_by_section.get(clip.get("section"), str(clip.get("clause") or ""))
            weights = keyword_tag_weights(text)
            overlap = tags.intersection(weights)
            semantic = float(sum(weights[tag] for tag in overlap))
            original_duration = float(clip["timeline_out"]) - float(clip["timeline_in"])
            fits = original_duration <= max(0.0, media_duration - 0.16)
            # File names are the approved content labels via manifest tags.
            score = semantic * 10.0 + (30.0 if overlap else 0.0) + (12.0 if fits else 0.0)
            pair_candidates.append((score, semantic, fits, media, index, sorted(overlap), text))
    pair_candidates.sort(key=lambda item: (-item[0], -item[1], not item[2], item[4]))

    used_replacements: set[int] = set()
    used_sections = {
        clip.get("section") for clip in broll
        if media_source_key(clip["source"]) in seen_common
    }
    injected = []
    desired_total = min(maximum, max(minimum, target))
    for score, semantic, _fits, media, index, overlap, text in pair_candidates:
        source_key = media_source_key(media["path"])
        if len(seen_common) >= desired_total:
            break
        if source_key in seen_common or index in used_replacements:
            continue
        original = broll[index]
        if original.get("section") in used_sections:
            continue
        media_duration = float(media.get("duration_seconds") or 0)
        original_duration = float(original["timeline_out"]) - float(original["timeline_in"])
        duration = min(original_duration, max(0.0, media_duration - 0.16))
        if duration < 1.5:
            continue
        source_in = max(0.08, (media_duration - duration) / 2)
        replacement = {
            **original,
            "source": str(Path(media["path"]).resolve()),
            "source_in": round(source_in, 3),
            "source_out": round(source_in + duration, 3),
            "timeline_out": round(float(original["timeline_in"]) + duration, 3),
            "description": media.get("description") or Path(media["path"]).stem,
            "origin": "skill_common_ai",
            "max_source_uses": 1,
            "clause": text,
            "matched_tags": overlap or list(media.get("tags") or []),
            "semantic_score": round(semantic, 2),
            "decision_score": round(score, 2),
            "confidence": "high" if overlap else "medium",
            "quota_injected": True,
            "content_label_source": "filename_manifest",
        }
        broll[index] = replacement
        seen_common.add(source_key)
        used_replacements.add(index)
        used_sections.add(original.get("section"))
        injected.append(
            {
                "source": replacement["source"],
                "section": replacement.get("section"),
                "replaced_source": original["source"],
                "matched_tags": replacement["matched_tags"],
            }
        )

    if len(seen_common) < minimum:
        raise ValueError(f"通用AI录屏仅排入 {len(seen_common)} 条，未达到最低 {minimum} 条。")
    plan["tracks"]["b_roll"] = sorted(
        broll, key=lambda item: (float(item["timeline_in"]), float(item["timeline_out"]))
    )
    return {
        "applied": True,
        "case_type": "ai",
        "minimum": minimum,
        "maximum": maximum,
        "target": desired_total,
        "used": len(seen_common),
        "injected": injected,
        "duplicates_or_excess_removed": removed,
        "content_label_source": "bundled filename manifest",
    }


def inject_common_ai_broll(
    project: Path,
    work: Path,
    plan_name: str,
    minimum: int,
    maximum: int,
    target: int,
) -> dict[str, Any]:
    plan_path = work / plan_name
    if not plan_path.is_file():
        raise FileNotFoundError(f"没有找到待更新计划：{plan_path}")
    inventory_path = (
        work / "media_inventory_with_charts.json"
        if (work / "media_inventory_with_charts.json").is_file()
        else work / "media_inventory.json"
    )
    if not inventory_path.is_file():
        raise FileNotFoundError("缺少媒体清单。")
    plan = load_json(plan_path)
    inventory = load_json(inventory_path)
    inventory["case_type"] = detect_project_case_type(project, find_script(project))
    existing = {media_source_key(item["path"]) for item in inventory.get("b_roll", [])}
    ffmpeg = find_ffmpeg()
    added = 0
    for common in load_common_ai_broll():
        key = media_source_key(common["path"])
        if key in existing:
            continue
        media = probe_media(common["path"], ffmpeg)
        media.update(
            {
                "origin": common["origin"],
                "description": common["description"],
                "tags": common["tags"],
            }
        )
        inventory.setdefault("b_roll", []).append(media)
        existing.add(key)
        added += 1
    report = ensure_common_ai_broll_quota(plan, inventory, minimum, maximum, target)
    duration = float(plan["timeline"]["duration_seconds"])
    metrics = dict(plan.get("b_roll_metrics", {}))
    metrics.update(timeline_coverage_metrics(plan["tracks"]["b_roll"], duration, 1.5))
    use_counts: dict[str, int] = {}
    for clip in plan["tracks"]["b_roll"]:
        key = media_source_key(clip["source"])
        use_counts[key] = use_counts.get(key, 0) + 1
    metrics.update(
        {
            "common_ai_recordings_used": report["used"],
            "common_ai_recording_minimum": minimum,
            "common_ai_recording_maximum": maximum,
            "common_ai_each_used_at_most_once": True,
            "maximum_observed_source_uses": max(use_counts.values(), default=0),
            "source_use_limit_respected": all(
                count <= max(
                    int(clip.get("max_source_uses", 2))
                    for clip in plan["tracks"]["b_roll"]
                    if media_source_key(clip["source"]) == source
                )
                for source, count in use_counts.items()
            ),
            "source_use_counts": dict(sorted(use_counts.items())),
        }
    )
    plan["b_roll_metrics"] = metrics
    summary = inventory.setdefault("summary", {})
    summary["b_roll_files"] = len(inventory.get("b_roll", []))
    summary["common_b_roll_files"] = sum(
        item.get("origin") == "skill_common_ai" for item in inventory.get("b_roll", [])
    )
    write_json(plan_path, plan)
    write_json(inventory_path, inventory)
    report["common_media_added_to_inventory"] = added
    report["plan"] = str(plan_path)
    report["inventory"] = str(inventory_path)
    write_json(work / "common_ai_broll_report.json", report)
    return report


def compose_edit_plan(
    work: Path,
    fps: float,
    bridge_gap_seconds: float,
    target_coverage: float,
) -> dict[str, Any]:
    roughcut_path = work / "roughcut_plan.json"
    matches_path = work / "broll_matches.json"
    if not roughcut_path.exists() or not matches_path.exists():
        raise FileNotFoundError("需要先生成 roughcut_plan.json 和 broll_matches.json。")
    roughcut = load_json(roughcut_path)
    matches = load_json(matches_path)
    inventory = load_json(work / "media_inventory.json")
    media_durations = {
        str(item["path"]).lower(): float(item.get("duration_seconds") or 0.0)
        for item in inventory.get("b_roll", [])
    }
    matches_by_section = {item["section"]: item["candidates"] for item in matches["sections"]}
    max_source_uses = max(1, int(matches.get("max_source_uses", 2)))
    composed_source_uses: dict[str, int] = {}
    cursor = 0.0
    aroll_track = []
    broll_track = []
    subtitle_track = []
    for section in roughcut["sections"]:
        if (
            section.get("status") == "missing"
            or section["source_in"] is None
            or section["source_out"] is None
        ):
            continue
        duration = max(0.0, float(section["source_out"]) - float(section["source_in"]))
        timeline_in = cursor
        timeline_out = cursor + duration
        aroll_track.append(
            {
                "section": section["section"],
                "speaker": section.get("speaker", "未标注人物"),
                "source": section["source"],
                "source_in": section["source_in"],
                "source_out": section["source_out"],
                "timeline_in": round(timeline_in, 3),
                "timeline_out": round(timeline_out, 3),
                "status": section["status"],
                "score": section["score"],
            }
        )
        subtitle_track.append(
            {
                "section": section["section"],
                "speaker": section.get("speaker", "未标注人物"),
                "text": section["script"],
                "timeline_in": round(timeline_in, 3),
                "timeline_out": round(timeline_out, 3),
            }
        )
        candidates = matches_by_section.get(section["section"], [])
        previous_overlay_end = timeline_in
        for candidate in sorted(candidates, key=lambda item: float(item.get("clause_start_fraction", 0.0))):
            source_key = str(Path(candidate["source"]).resolve()).lower()
            source_limit = max(1, int(candidate.get("max_source_uses", max_source_uses)))
            if composed_source_uses.get(source_key, 0) >= source_limit:
                continue
            overlay_duration = max(0.0, float(candidate["source_out"]) - float(candidate["source_in"]))
            overlay_duration = min(overlay_duration, max(duration - 0.6, 0.0))
            if overlay_duration < 0.8:
                continue
            clause_start = timeline_in + duration * float(candidate.get("clause_start_fraction", 0.0))
            clause_end = timeline_in + duration * float(candidate.get("clause_end_fraction", 1.0))
            clause_center = (clause_start + clause_end) / 2
            overlay_in = max(timeline_in + 0.3, clause_center - overlay_duration / 2)
            overlay_in = max(overlay_in, previous_overlay_end)
            overlay_in = min(overlay_in, timeline_out - 0.3)
            available = timeline_out - 0.3 - overlay_in
            overlay_duration = min(overlay_duration, available)
            if overlay_duration < 1.2:
                continue
            previous_overlay_end = overlay_in + overlay_duration
            broll_track.append(
                {
                    "section": section["section"],
                    "source": candidate["source"],
                    "source_in": candidate["source_in"],
                    "source_out": round(float(candidate["source_in"]) + overlay_duration, 3),
                    "timeline_in": round(overlay_in, 3),
                    "timeline_out": round(overlay_in + overlay_duration, 3),
                    "description": candidate["description"],
                    "origin": candidate.get("origin", "project"),
                    "max_source_uses": source_limit,
                    "clause": candidate.get("clause", ""),
                    "confidence": candidate["confidence"],
                    "matched_tags": candidate["matched_tags"],
                    "semantic_score": candidate.get("semantic_score"),
                    "quality_score": candidate.get("quality_score"),
                    "decision_score": candidate.get("decision_score"),
                }
            )
            composed_source_uses[source_key] = composed_source_uses.get(source_key, 0) + 1
        cursor = timeline_out
    provisional_plan = {"tracks": {"b_roll": broll_track, "subtitles": subtitle_track}}
    common_quota = ensure_common_ai_broll_quota(provisional_plan, inventory)
    broll_track = provisional_plan["tracks"]["b_roll"]
    composed_source_uses = {}
    for clip in broll_track:
        key = media_source_key(clip["source"])
        composed_source_uses[key] = composed_source_uses.get(key, 0) + 1
    metrics_before = timeline_coverage_metrics(broll_track, cursor, bridge_gap_seconds)
    bridged_gaps = bridge_short_broll_gaps(
        broll_track,
        media_durations,
        bridge_gap_seconds,
    )
    metrics_after = timeline_coverage_metrics(broll_track, cursor, bridge_gap_seconds)
    metrics_after.update(
        {
            "target_coverage_ratio": round(target_coverage, 4),
            "target_met": metrics_after["coverage_ratio"] >= target_coverage,
            "bridged_gaps": bridged_gaps,
            "coverage_before_bridging": metrics_before["coverage_ratio"],
            "max_source_uses": max_source_uses,
            "common_ai_max_source_uses": 1,
            "common_ai_quota": common_quota,
            "maximum_observed_source_uses": max(composed_source_uses.values(), default=0),
            "source_use_limit_respected": all(
                count <= max(
                    int(clip.get("max_source_uses", max_source_uses))
                    for clip in broll_track if media_source_key(clip["source"]) == source
                )
                for source, count in composed_source_uses.items()
            ),
            "unique_sources_used": len(composed_source_uses),
            "source_use_counts": {
                source: count for source, count in sorted(composed_source_uses.items())
            },
        }
    )
    result = {
        "schema_version": 2,
        "timeline": {"fps": fps, "width": 1920, "height": 1080, "duration_seconds": round(cursor, 3)},
        "tracks": {
            "a_roll": aroll_track,
            "b_roll": broll_track,
            "subtitles": subtitle_track,
        },
        "b_roll_metrics": metrics_after,
        "review_required": any(item["status"] != "matched" for item in aroll_track)
        or any(item["confidence"] != "high" for item in broll_track)
        or not metrics_after["target_met"],
    }
    write_json(work / "edit_plan.json", result)
    return result


def print_summary(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="标杆案例视频自动剪辑 MVP")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--project", type=Path, required=True, help="只读素材目录")
        subparser.add_argument("--work", type=Path, required=True, help="生成文件目录")

    inspect_cmd = subparsers.add_parser("inspect", help="检查素材并生成清单")
    common(inspect_cmd)

    thumb_cmd = subparsers.add_parser("thumbnails", help="生成 B-roll 代表画面")
    common(thumb_cmd)
    thumb_cmd.add_argument("--width", type=int, default=640)

    analyze_broll_cmd = subparsers.add_parser("analyze-broll", help="分析 B-roll 稳定度并选择可用片段")
    common(analyze_broll_cmd)
    analyze_broll_cmd.add_argument("--sample-fps", type=float, default=4.0)
    analyze_broll_cmd.add_argument("--workers", type=int, default=4)

    transcribe_cmd = subparsers.add_parser("transcribe", help="本地转写 A-roll")
    common(transcribe_cmd)
    transcribe_cmd.add_argument("--model", default="small")
    transcribe_cmd.add_argument("--language", default="zh")
    transcribe_cmd.add_argument("--device", default="cpu")
    transcribe_cmd.add_argument("--compute-type", default="int8")

    build_v1_cmd = subparsers.add_parser(
        "build-v1",
        help="忽略V0，直接从全部A-roll转写去重并生成V1",
    )
    common(build_v1_cmd)
    build_v1_cmd.add_argument("--speaker-map", type=Path)
    build_v1_cmd.add_argument("--corrections", type=Path)
    build_v1_cmd.add_argument("--duplicate-threshold", type=float, default=0.48)
    build_v1_cmd.add_argument(
        "--sentence-target-chars",
        type=int,
        default=38,
        help="转写句段的目标字数；较短句段能让数据保护和主讲人配额更精确",
    )

    prepare_correction_cmd = subparsers.add_parser(
        "prepare-v1-correction",
        help="为V1生成带前后文和时间码的AI语句校对清单",
    )
    common(prepare_correction_cmd)
    prepare_correction_cmd.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.82,
        help="低于该转写置信度时标为高优先级复核",
    )

    correct_v1_cmd = subparsers.add_parser(
        "correct-v1",
        help="安全应用AI审阅后的V1语句校正计划",
    )
    common(correct_v1_cmd)
    correct_v1_cmd.add_argument("--review", type=Path, required=True)
    correct_v1_cmd.add_argument(
        "--min-context-similarity",
        type=float,
        default=0.55,
        help="仅凭上下文校正时，与原始转写必须达到的最低相似度",
    )

    shorten_cmd = subparsers.add_parser(
        "shorten-script",
        help="按完整语义段把校正后V1压缩为5至6.5分钟V2",
    )
    common(shorten_cmd)
    shorten_cmd.add_argument("--primary-speaker")
    shorten_cmd.add_argument("--target-minutes", type=float, default=5.5)
    shorten_cmd.add_argument(
        "--max-minutes",
        type=float,
        default=6.5,
        help="成片最长时长；默认允许完整语义段把成片放宽到6分30秒",
    )
    shorten_cmd.add_argument("--min-chars", type=int, default=1500)
    shorten_cmd.add_argument("--max-chars", type=int, default=1800)

    align_cmd = subparsers.add_parser("align", help="讲稿与转写结果对齐")
    common(align_cmd)
    align_cmd.add_argument("--accept-score", type=float, default=0.78)
    align_cmd.add_argument("--review-score", type=float, default=0.52)
    align_cmd.add_argument("--padding", type=float, default=0.12)

    preview_cmd = subparsers.add_parser("preview", help="渲染 A-roll 预览")
    common(preview_cmd)
    preview_cmd.add_argument("--min-score", type=float, default=0.52)
    preview_cmd.add_argument("--crf", type=int, default=24)

    sheets_cmd = subparsers.add_parser("contact-sheets", help="生成 B-roll 联系表")
    common(sheets_cmd)
    sheets_cmd.add_argument("--columns", type=int, default=4)
    sheets_cmd.add_argument("--rows", type=int, default=4)

    labels_cmd = subparsers.add_parser("apply-labels", help="把人工或模型标签合并进 B-roll 索引")
    common(labels_cmd)
    labels_cmd.add_argument("--labels", type=Path, required=True)

    match_cmd = subparsers.add_parser("match-broll", help="按讲稿关键词生成 B-roll 候选")
    common(match_cmd)
    match_cmd.add_argument("--clips-per-section", type=int, default=4)
    match_cmd.add_argument("--clip-seconds", type=float, default=4.5)
    match_cmd.add_argument(
        "--max-source-uses",
        type=int,
        default=2,
        help="同一条 B-roll 源素材在全片中的最多使用次数",
    )

    compose_cmd = subparsers.add_parser("compose", help="生成多轨时间线编辑计划")
    common(compose_cmd)
    compose_cmd.add_argument("--fps", type=float, default=25.0)
    compose_cmd.add_argument(
        "--bridge-gap-seconds",
        type=float,
        default=1.5,
        help="自动连接不超过该秒数的相邻 B-roll，避免短暂闪回 A-roll",
    )
    compose_cmd.add_argument(
        "--target-coverage",
        type=float,
        default=0.50,
        help="B-roll 最低覆盖率目标；未达到时在结果中标记复核",
    )
    inject_common_cmd = subparsers.add_parser(
        "inject-common-broll",
        help="为已有AI案例时间线补足3至5条不重复的通用产品录屏",
    )
    common(inject_common_cmd)
    inject_common_cmd.add_argument("--plan-name", default="edit_plan_with_charts.json")
    inject_common_cmd.add_argument("--minimum", type=int, default=3)
    inject_common_cmd.add_argument("--maximum", type=int, default=5)
    inject_common_cmd.add_argument("--target", type=int, default=4)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project = args.project.resolve()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    if project == work or project in work.parents:
        raise ValueError("工作目录不能等于素材目录，也不能位于素材目录内部。")

    if args.command == "inspect":
        print_summary(inspect_project(project, work)["summary"])
    elif args.command == "thumbnails":
        result = make_thumbnails(project, work, args.width)
        print_summary({"clips": len(result["clips"]), "index": str(work / "broll_index.json")})
    elif args.command == "analyze-broll":
        result = analyze_broll(project, work, args.sample_fps, args.workers)
        print_summary(
            {
                "clips": len(result["clips"]),
                "failures": len(result["failures"]),
                "analysis": str(work / "broll_analysis.json"),
            }
        )
    elif args.command == "transcribe":
        result = transcribe_aroll(project, work, args.model, args.language, args.device, args.compute_type)
        print_summary({"files": len(result["files"]), "transcript": str(work / "transcript.json")})
    elif args.command == "build-v1":
        result = build_v1_from_transcript(
            work,
            args.speaker_map.resolve() if args.speaker_map else None,
            args.corrections.resolve() if args.corrections else None,
            args.duplicate_threshold,
            args.sentence_target_chars,
        )
        print_summary(
            {
                "speakers": result["speaker_order"],
                "raw_sentences": result["raw_sentence_count"],
                "deduplicated_sentences": result["deduplicated_sentence_count"],
                "duplicates_removed": len(result["duplicates_removed"]),
                "script_v1": str(work / "script_v1.json"),
            }
        )
    elif args.command == "prepare-v1-correction":
        result = prepare_v1_correction(work, args.confidence_threshold)
        print_summary(
            {
                "sentences": len(result["rows"]),
                "high_priority": sum(
                    1 for item in result["rows"] if item["priority"] == "high"
                ),
                "worksheet": str(work / "v1_correction_worksheet.json"),
            }
        )
    elif args.command == "correct-v1":
        result = apply_v1_corrections(
            work,
            args.review.resolve(),
            args.min_context_similarity,
        )
        print_summary(
            {
                "corrections_applied": result["corrections_applied"],
                "unresolved_audio_reviews": len(result["unresolved_audio_reviews"]),
                "ready_for_v2": result["ready_for_v2"],
                "script_v1_corrected": str(work / "script_v1_corrected.json"),
            }
        )
    elif args.command == "shorten-script":
        result = shorten_script(
            work,
            args.primary_speaker,
            args.target_minutes,
            args.max_minutes,
            args.min_chars,
            args.max_chars,
        )
        print_summary(
            {
                "primary_speaker": result["primary_speaker"],
                "primary_selection_basis": result["primary_selection_basis"],
                "selected_chars": result["selected_chars"],
                "estimated_minutes": result["estimated_minutes"],
                "within_target_range": result["within_target_range"],
                "speaker_distribution": result["speaker_distribution"],
                "script_v2": str(work / "script_v2.json"),
            }
        )
    elif args.command == "align":
        result = align_script(project, work, args.accept_score, args.review_score, args.padding)
        print_summary({"sections": len(result["sections"]), "plan": str(work / "roughcut_plan.json")})
    elif args.command == "preview":
        print(render_preview(project, work, args.min_score, args.crf))
    elif args.command == "contact-sheets":
        outputs = create_contact_sheets(work, args.columns, args.rows)
        print_summary({"sheets": len(outputs), "folder": str(work / "broll_contact_sheets")})
    elif args.command == "apply-labels":
        print_summary(apply_broll_labels(work, args.labels.resolve()))
    elif args.command == "match-broll":
        result = match_broll(
            work,
            args.clips_per_section,
            args.clip_seconds,
            args.max_source_uses,
        )
        print_summary(
            {
                "sections": len(result["sections"]),
                "max_source_uses": result["max_source_uses"],
                "unique_sources_used": len(result["source_use_counts"]),
                "sources_at_limit": len(result["sources_at_limit"]),
                "matches": str(work / "broll_matches.json"),
            }
        )
    elif args.command == "compose":
        result = compose_edit_plan(work, args.fps, args.bridge_gap_seconds, args.target_coverage)
        print_summary(
            {
                "duration_seconds": result["timeline"]["duration_seconds"],
                "a_roll_clips": len(result["tracks"]["a_roll"]),
                "b_roll_clips": len(result["tracks"]["b_roll"]),
                "b_roll_metrics": result["b_roll_metrics"],
                "plan": str(work / "edit_plan.json"),
            }
        )
    elif args.command == "inject-common-broll":
        print_summary(
            inject_common_ai_broll(
                project,
                work,
                args.plan_name,
                args.minimum,
                args.maximum,
                args.target,
            )
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
