"""Speech-anchored chart timing and deterministic animation templates.

Content selection is reviewed by the invoking agent. Timing is derived from
the retained A-roll words, never from a new reading of the script.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image, ImageDraw

import chart_pipeline as cp
from pipeline import find_ffmpeg, normalize_text


def canonical(text):
    """Return normalized characters; keep 万 as a unit, normalize 十四 to 14."""
    text = normalize_text(text)
    digits = dict(zip("零一二三四五六七八九两", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2]))

    def number(match):
        value = match.group()
        if not any(c in value for c in "十百千"):
            return "".join(str(digits[c]) for c in value)
        total = current = 0
        for char in value:
            if char in digits:
                current = digits[char]
            else:
                total += (current or 1) * {"十": 10, "百": 100, "千": 1000}[char]
                current = 0
        return str(total + current)

    return re.sub(r"[零一二三四五六七八九两十百千]+", number, text)


def timed_characters(words):
    chars, spans = [], []
    for word in words:
        value = normalize_text(word["word"])
        for index, char in enumerate(value):
            length = max(1, len(value))
            start, end = float(word["start"]), float(word["end"])
            chars.append(char)
            spans.append((start + (end - start) * index / length,
                          start + (end - start) * (index + 1) / length))
    text = "".join(chars)
    # Normalize numeric runs across ASR word boundaries, preserving their span.
    result, result_spans, cursor = [], [], 0
    for match in re.finditer(r"[零一二三四五六七八九两十百千]+", text):
        result.extend(text[cursor:match.start()])
        result_spans.extend(spans[cursor:match.start()])
        converted = canonical(match.group())
        result.extend(converted)
        result_spans.extend([(spans[match.start()][0], spans[match.end() - 1][1])] * len(converted))
        cursor = match.end()
    result.extend(text[cursor:])
    result_spans.extend(spans[cursor:])
    return "".join(result), result_spans


def expected_keys(chart):
    if chart["template"] == "stat":
        return ["value"]
    if chart["template"] == "comparison":
        return [f"row_{i}_{side}" for i, _ in enumerate(chart["rows"], 1) for side in ("before", "after")]
    return [f"item_{i}" for i, _ in enumerate(chart["items"], 1)]


def suggested_cues(chart, subtitles):
    if chart["template"] == "stat":
        texts = [("value", chart["value"])]
    elif chart["template"] == "comparison":
        texts = [(f"row_{i}_{side}", row[side]) for i, row in enumerate(chart["rows"], 1)
                 for side in ("before", "after")]
    else:
        texts = [(f"item_{i}", text) for i, text in enumerate(chart["items"], 1)]
    result = []
    for key, quote in texts:
        matches = [s for s in chart["section_ids"] if canonical(quote) in canonical(subtitles[int(s)]["text"])]
        result.append({"key": key, "section": matches[0] if len(matches) == 1 else None,
                       "quote": quote, "needs_quote_review": len(matches) != 1})
    return result


def locate_cue(cue, subtitle, clip, transcript_file):
    target, quote = canonical(subtitle["text"]), canonical(cue["quote"])
    locations = [m.start() for m in re.finditer(re.escape(quote), target)] if quote else []
    occurrence = cue.get("occurrence")
    if not locations or (len(locations) != 1 and occurrence is None):
        raise ValueError(f"锚点不存在或重复，须指定 occurrence：{cue}")
    position = locations[int(occurrence or 1) - 1]
    source_in, source_out = float(clip["source_in"]), float(clip["source_out"])
    words = [w for segment in transcript_file.get("segments", []) for w in segment.get("words", [])
             if float(w["end"]) > source_in and float(w["start"]) < source_out]
    raw, spans = timed_characters(words)
    correspondence = {}
    for block in SequenceMatcher(None, target, raw, autojunk=False).get_matching_blocks():
        correspondence.update({block.a + i: block.b + i for i in range(block.size)})
    indices = [correspondence[p] for p in range(position, position + len(quote)) if p in correspondence]
    coverage = len(indices) / max(1, len(quote))
    boundary_ok = position in correspondence and position + len(quote) - 1 in correspondence
    if not indices or coverage < 0.65 or not boundary_ok:
        raise ValueError(f"锚点逐词对齐不足，须换用已说出的短语或复听：{cue['quote']} 覆盖率 {coverage:.0%}")
    start, end = max(source_in, spans[min(indices)][0]), min(source_out, spans[max(indices)][1])
    if end <= start:
        raise ValueError(f"锚点时间为空：{cue}")
    # Current exporter preserves speed 1 for A-roll. Reject changed speed.
    if abs((source_out - source_in) - (float(clip["timeline_out"]) - float(clip["timeline_in"]))) > 0.05:
        raise ValueError("当前逐词同步要求 A-roll 原速播放。")
    return {**cue, "word_coverage": round(coverage, 3), "method": "retained_asr_words",
            "source": clip["source"], "source_in": round(start, 4), "source_out": round(end, 4),
            "timeline_in": round(float(clip["timeline_in"]) + start - source_in, 4),
            "timeline_out": round(float(clip["timeline_in"]) + end - source_in, 4)}


def sync_charts(work):
    plan_path, edit_path, transcript_path = (work / name for name in ("chart_plan.json", "edit_plan.json", "transcript.json"))
    plan, edit, transcript = map(cp.load_json, (plan_path, edit_path, transcript_path))
    cp.validate_chart_plan(plan, edit_path)
    subtitles = {int(s["section"]): s for s in edit["tracks"]["subtitles"]}
    clips = {int(c["section"]): c for c in edit["tracks"]["a_roll"]}
    files = {os.path.normcase(str(Path(f["source"]).resolve())): f for f in transcript["files"]}
    fps = float(edit["timeline"]["fps"])
    reports, errors = [], []
    for chart in plan["charts"]:
        try:
            cues = chart.get("cues") or suggested_cues(chart, subtitles)
            if sorted(c["key"] for c in cues) != sorted(expected_keys(chart)):
                raise ValueError("每个数值或步骤必须有且只有一个口播锚点。")
            resolved = []
            for cue in cues:
                section = int(cue["section"])
                if section not in chart["section_ids"]:
                    raise ValueError("锚点来自图表未引用的句段。")
                clip = clips[section]
                file = files[os.path.normcase(str(Path(clip["source"]).resolve()))]
                item = locate_cue(cue, subtitles[section], clip, file)
                item["reveal_at"] = round(round(item["timeline_in"] * fps) / fps, 4)
                resolved.append(item)
            if chart["template"] in ("flow", "list"):
                ordered = [next(c["reveal_at"] for c in resolved if c["key"] == key) for key in expected_keys(chart)]
                if ordered != sorted(ordered):
                    raise ValueError("列表或流程的顺序与实际口播不一致。")
            source_start = min(subtitles[int(s)]["timeline_in"] for s in chart["section_ids"])
            source_end = max(subtitles[int(s)]["timeline_out"] for s in chart["section_ids"])
            # When the second number finishes a speaker's last sentence,
            # reveal its comparison pair together at the start of that clause.
            # Keep exact word times in the report; do not spill onto a new speaker.
            for i, _ in enumerate(chart.get("rows", []), 1):
                pair = [c for c in resolved if c["key"] in (f"row_{i}_before", f"row_{i}_after")]
                if pair and source_end - max(c["reveal_at"] for c in pair) < 2.8:
                    pair_start = min(c["reveal_at"] for c in pair)
                    if source_end - pair_start >= 1.5:
                        for c in pair:
                            c["reveal_at"] = pair_start
                            c["readability_adjustment"] = "comparison_pair_at_clause_start"
            start = max(math.ceil(source_start * fps) / fps, math.floor((min(c["reveal_at"] for c in resolved) - 0.4) * fps) / fps)
            end = min(math.floor(source_end * fps) / fps,
                      math.ceil(max(start + 6.0, max(c["timeline_out"] for c in resolved) + 2.0) * fps) / fps)
            start = max(math.ceil(source_start * fps) / fps, min(start, math.floor((end - 5.0) * fps) / fps))
            if end - max(c["reveal_at"] for c in resolved) < 1.0:
                raise ValueError("最后一项显示不足 1 秒，需调整图表内容或引用的连续句段。")
            chart["timeline_in"], chart["timeline_out"] = round(start, 4), round(end, 4)
            chart["cues"] = [{k: v for k, v in c.items() if k != "needs_quote_review"} for c in cues]
            chart["synced_cues"] = resolved
            reports.append({"id": chart["id"], "title": chart["title"], "start": start, "end": end, "cues": resolved})
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            errors.append({"id": chart["id"], "error": str(exc)})
    cp.write_json(work / "chart_sync_report.json", {"charts": reports, "errors": errors, "review_required": bool(errors)})
    if errors:
        raise ValueError("部分图表无法可靠同步，详见 chart_sync_report.json；未写入同步计划。")
    plan["motion"] = {"enabled": True, "fps": fps, "timing": "retained_asr_words"}
    plan["source_transcript_sha256"] = cp.file_sha256(transcript_path)
    cp.validate_chart_plan(plan, edit_path)
    cp.write_json(plan_path, plan)
    lines = ["# 动态图表口播同步", "", "时间来自保留的 A-roll 逐词转写；以下为成片时间。", ""]
    for item in reports:
        lines.extend([f"## {item['id']} {item['title']}", "", f"显示：{item['start']:.2f}–{item['end']:.2f} 秒", ""])
        lines.extend(f"- {c['key']}：锚点“{c['quote']}”，原词 {c['timeline_in']:.2f} 秒，显示 {c['reveal_at']:.2f} 秒；匹配 {c['word_coverage']:.0%}" + ("；句尾对比按整句同时显示" if c.get("readability_adjustment") else "") for c in item["cues"])
        lines.append("")
    (work / "chart_sync_report.md").write_text("\n".join(lines), encoding="utf-8")
    return {"charts": len(reports), "cues": sum(len(r["cues"]) for r in reports), "review_required": False}


def motion_fingerprint(plan):
    return cp.hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate_synced(plan, work):
    cp.validate_chart_plan(plan, work / "edit_plan.json")
    if plan.get("source_transcript_sha256") != cp.file_sha256(work / "transcript.json"):
        raise ValueError("逐词转写已变化，请重新 sync。")
    for chart in plan["charts"]:
        cues = chart.get("synced_cues", [])
        if sorted(c["key"] for c in cues) != sorted(expected_keys(chart)):
            raise ValueError(f"图表 {chart['id']} 缺少同步锚点，请先 sync。")
        if [{k: c[k] for k in ("key", "section", "quote")} for c in cues] != [{k: c[k] for k in ("key", "section", "quote")} for c in chart["cues"]]:
            raise ValueError("口播锚点修改后需要重新 sync。")
        for cue in cues:
            if not chart["timeline_in"] <= cue["reveal_at"] <= chart["timeline_out"] - 1:
                raise ValueError("图表锚点超出可读时间范围。")


def blank():
    return Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))


def draw_label(image, box, text, color, size=50, minimum=26, bold=True):
    draw = ImageDraw.Draw(image)
    used_font = cp.fit_font(draw, str(text), box[2] - box[0] - 30, size, minimum, bold)
    if draw.textbbox((0, 0), str(text), font=used_font)[2] > box[2] - box[0] - 20:
        raise ValueError(f"文字超出模板，请精简：{text}")
    cp.draw_centered(draw, box, str(text), used_font, color)


def editorial_panel(image, box, blue):
    """A compact, readable overlay that leaves the footage visible around it."""
    d = ImageDraw.Draw(image)
    left, top, right, bottom = box
    d.rounded_rectangle((left + 12, top + 14, right + 12, bottom + 14), 38,
                        fill=(4, 11, 24, 82))
    d.rounded_rectangle(box, 38, fill=(12, 27, 48, 224),
                        outline=(135, 183, 225, 175), width=3)
    d.rounded_rectangle((left + 35, top + 34, left + 47, top + 105), 6, fill=blue)


def editorial_layers_for_chart(chart, style):
    """V13 visual theme; keep the same layer keys and speech anchors as V12."""
    blue = style.get("brand_color", "#1685F8")
    accent = style.get("accent_color", "#FF8A18")
    white, muted = "#F7FBFF", "#BCD0E2"
    base, result = blank(), []
    d = ImageDraw.Draw(base)
    template = chart["template"]
    if template == "stat":
        left = 1000 if chart.get("placement") == "right" else 100
        right = left + 820
        editorial_panel(base, (left, 100, right, 875), blue)
        d.rounded_rectangle((left + 190, 164, right - 190, 170), 3, fill=blue)
        draw_label(base, (left + 80, 188, right - 80, 260), chart["title"], white, 60)
        d.line((left + 105, 757, right - 105, 757), fill=(130, 174, 215, 115), width=3)
        draw_label(base, (left + 90, 771, right - 90, 836), chart.get("caption", ""), muted, 41)
        value = blank()
        text = str(chart["value"])
        if len(text) > 7:
            split = len(text) // 2
            draw_label(value, (left + 240, 399, right - 240, 494), text[:split], "#FFBD79", 80)
            draw_label(value, (left + 240, 495, right - 240, 590), text[split:], "#FFBD79", 80)
        else:
            draw_label(value, (left + 220, 405, right - 220, 588), text, "#FFBD79", 110, 40)
        result.append(("value", value))
    elif template == "comparison":
        rows = chart["rows"]
        left = 1000 if chart.get("placement") == "right" else 100
        right = left + 840
        bottom = 465 + (len(rows) - 1) * 185 + 60
        editorial_panel(base, (left, 78, right, bottom), blue)
        draw_label(base, (left + 85, 132, right - 55, 222), chart["title"], white, 57)
        d.line((left + 70, 246, right - 70, 246), fill=(130, 174, 215, 120), width=3)
        for i, row in enumerate(rows, 1):
            y = 265 + (i - 1) * 185
            draw_label(base, (left + 75, y, right - 75, y + 48), row["label"], white, 39)
            draw_label(base, (left + 70, y + 53, left + 380, y + 92), chart["columns"][0], muted, 34)
            draw_label(base, (left + 470, y + 53, right - 60, y + 92), chart["columns"][1], "#79C2FF", 34)
            d.rounded_rectangle((left + 65, y + 105, left + 380, y + 200), 18,
                                fill=(30, 53, 78, 255), outline=(190, 216, 239, 215), width=2)
            d.rounded_rectangle((left + 470, y + 105, right - 55, y + 200), 18,
                                fill=(17, 100, 184, 248), outline=(99, 190, 255, 235), width=2)
            d.line((left + 404, y + 152, left + 435, y + 152), fill=accent, width=5)
            d.polygon([(left + 432, y + 140), (left + 452, y + 152),
                       (left + 432, y + 164)], fill=accent)
            for side, box, color in [
                ("before", (left + 80, y + 112, left + 365, y + 193), white),
                ("after", (left + 485, y + 112, right - 70, y + 193), "#FFFFFF"),
            ]:
                layer = blank()
                draw_label(layer, box, row[side], color, 70, 31)
                result.append((f"row_{i}_{side}", layer))
    else:
        left = 980 if chart.get("placement") == "right" else 100
        right = left + 840
        count = len(chart["items"])
        height = min(116, (590 - (count - 1) * 38) // count)
        top, gap = 263, 38
        bottom = top + (count - 1) * (height + gap) + height + 55
        editorial_panel(base, (left, 75, right, bottom), blue)
        draw_label(base, (left + 85, 125, right - 55, 220), chart["title"], white, 55)
        d.line((left + 88, 235, right - 70, 235), fill=(130, 174, 215, 125), width=3)
        for i, text in enumerate(chart["items"], 1):
            layer = blank()
            ld = ImageDraw.Draw(layer)
            y = top + (i - 1) * (height + gap)
            ld.rounded_rectangle((left + 58, y, right - 58, y + height), 20,
                                 fill=(255, 255, 255, 28),
                                 outline=(130, 177, 220, 100), width=2)
            ld.rounded_rectangle((left + 76, y + 17, left + 158, y + height - 17), 17,
                                 fill=blue)
            draw_label(layer, (left + 82, y + 17, left + 152, y + height - 17),
                       f"{i:02d}", white, 34)
            draw_label(layer, (left + 172, y + 8, right - 70, y + height - 8),
                       text, white, 43, 27)
            if template == "flow" and i > 1:
                x = left + 117
                ld.line((x, y - 31, x, y - 8), fill=accent, width=6)
                ld.polygon([(x - 9, y - 13), (x + 9, y - 13), (x, y - 3)], fill=accent)
            result.append((f"item_{i}", layer))
    return base, result


def _bilingual_text(chart, key):
    value = chart.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"双语图表 {chart.get('id', chart.get('title', ''))} 缺少已审核字段 {key}")
    return value.strip()


def _bilingual_mask(alpha=122):
    """Full-frame readability veil; logos and captions are composited above it later."""
    image = blank()
    ImageDraw.Draw(image).rectangle((0, 0, 1919, 1079), fill=(4, 14, 24, alpha))
    return image


def _bilingual_heading(image, chart, left, right, top, chinese_size=56):
    d = ImageDraw.Draw(image)
    white, muted = "#F7FBFF", "#BFE4E7"
    draw_label(image, (left + 55, top, right - 55, top + 76), chart["title"], white, chinese_size, 30)
    draw_label(image, (left + 70, top + 72, right - 70, top + 111),
               _bilingual_text(chart, "title_en"), muted, 25, 15)
    cx = (left + right) // 2
    line_y = top + 124
    d.line((cx - 165, line_y, cx + 165, line_y), fill=(205, 239, 243, 145), width=2)
    d.ellipse((cx - 5, line_y - 5, cx + 5, line_y + 5), fill=(150, 236, 223, 230))


def bilingual_layers_for_chart(chart, style):
    """Fine-line bilingual theme with explicit non-overlapping text zones."""
    blue = style.get("brand_color", "#1685F8")
    white, muted, mint = "#F7FBFF", "#BFE4E7", "#91F0DF"
    base, result = blank(), []
    d = ImageDraw.Draw(base)
    template = chart["template"]
    left = 1000 if chart.get("placement") == "right" else 100
    right = left + 840
    if chart.get("layout", {}).get("blur"):
        result.append(("full_mask", _bilingual_mask()))
    if template == "stat":
        d.rounded_rectangle((left, 72, right, 920), 36, fill=(7, 29, 41, 118),
                            outline=(194, 235, 237, 150), width=2)
        _bilingual_heading(base, chart, left, right, 110, 57)
        draw_label(base, (left + 90, 760, right - 90, 810), chart.get("caption", ""), white, 38, 24)
        draw_label(base, (left + 105, 810, right - 105, 850),
                   _bilingual_text(chart, "caption_en"), muted, 23, 14)
        value = blank()
        chinese = str(chart["value"])
        if len(chinese) > 7:
            split = len(chinese) // 2
            draw_label(value, (left + 210, 412, right - 210, 500), chinese[:split], white, 82, 38)
            draw_label(value, (left + 210, 500, right - 210, 588), chinese[split:], white, 82, 38)
            english_top = 610
        else:
            draw_label(value, (left + 185, 430, right - 185, 570), chinese, white, 104, 42)
            english_top = 586
        draw_label(value, (left + 170, english_top, right - 170, english_top + 45),
                   _bilingual_text(chart, "value_en"), mint, 28, 16)
        result.append(("value", value))
    elif template in ("list", "flow"):
        items_en = chart.get("items_en")
        if not isinstance(items_en, list) or len(items_en) != len(chart["items"]):
            raise ValueError(f"双语图表 {chart.get('id')} 的 items_en 必须逐项对应中文")
        count = len(chart["items"])
        row_h, gap, top = 126, 25, 278
        bottom = top + count * row_h + (count - 1) * gap + 42
        d.rounded_rectangle((left, 72, right, bottom), 34, fill=(7, 29, 41, 210),
                            outline=(194, 235, 237, 165), width=2)
        _bilingual_heading(base, chart, left, right, 105, 53)
        for index, (item, english) in enumerate(zip(chart["items"], items_en), 1):
            layer = blank()
            ld = ImageDraw.Draw(layer)
            y = top + (index - 1) * (row_h + gap)
            ld.rounded_rectangle((left + 48, y, right - 48, y + row_h), 18,
                                 fill=(17, 49, 62, 176), outline=(208, 240, 241, 135), width=2)
            ld.ellipse((left + 72, y + 34, left + 126, y + 88),
                       outline=(196, 244, 235, 235), width=3)
            draw_label(layer, (left + 74, y + 36, left + 124, y + 86),
                       f"{index:02d}", white, 22, 16)
            draw_label(layer, (left + 145, y + 13, right - 72, y + 67), item, white, 39, 25)
            draw_label(layer, (left + 145, y + 70, right - 72, y + 111),
                       str(english), muted, 21, 13)
            if template == "flow" and index > 1:
                x = left + 99
                ld.line((x, y - 21, x, y - 6), fill=(150, 236, 223, 220), width=4)
                ld.polygon([(x - 7, y - 10), (x + 7, y - 10), (x, y - 1)], fill=(150, 236, 223, 230))
            result.append((f"item_{index}", layer))
    else:
        raise ValueError(f"双语侧边模板暂不支持：{template}")
    return base, result


def bilingual_fullscreen_comparison(chart, style):
    """One-row full-screen before/after card with full-height readability veil."""
    rows = chart["rows"]
    if len(rows) != 1:
        raise ValueError("双语前后对比每页只放一行数据 请拆分后再渲染")
    row = rows[0]
    for key in ("label_en", "before_en", "after_en"):
        if not row.get(key):
            raise ValueError(f"双语对比 {chart.get('id')} 缺少 {key}")
    columns_en = chart.get("columns_en")
    if not isinstance(columns_en, list) or len(columns_en) != 2:
        raise ValueError(f"双语对比 {chart.get('id')} 的 columns_en 必须有两项")
    base, layers = _bilingual_mask(132), []
    d = ImageDraw.Draw(base)
    _bilingual_heading(base, chart, 240, 1680, 78, 68)
    draw_label(base, (690, 226, 1230, 274), row["label"], "#F7FBFF", 34, 22)
    draw_label(base, (720, 270, 1200, 306), row["label_en"], "#BFE4E7", 21, 13)
    centers = (590, 1330)
    for index, cx in enumerate(centers):
        cy, radius = 605, 224
        if index == 0:
            d.ellipse((cx-radius, cy-radius, cx+radius, cy+radius), outline=(217,249,246,230), width=5)
            d.ellipse((cx-radius+25, cy-radius+25, cx+radius-25, cy+radius-25),
                      outline=(217,249,246,90), width=2)
            for tick in range(32):
                angle = math.radians(tick * 11.25 - 90)
                inner, outer = radius + 13, radius + (30 if tick % 4 == 0 else 22)
                d.line((cx+inner*math.cos(angle), cy+inner*math.sin(angle),
                        cx+outer*math.cos(angle), cy+outer*math.sin(angle)),
                       fill=(217,249,246,120), width=2)
        else:
            for segment in range(12):
                start = -90 + segment * 30
                d.arc((cx-radius, cy-radius, cx+radius, cy+radius), start, start + 22,
                      fill=(145,240,223,235) if segment % 3 else (255,177,164,235), width=10)
            d.ellipse((cx-radius+35, cy-radius+35, cx+radius-35, cy+radius-35),
                      outline=(217,249,246,105), width=3)
        draw_label(base, (cx-230, 396, cx+230, 451), chart["columns"][index], "#F7FBFF", 38, 24)
        draw_label(base, (cx-220, 450, cx+220, 485), columns_en[index], "#BFE4E7", 21, 13)
    d.line((914, 610, 982, 610), fill=(233,251,251,220), width=4)
    d.polygon(((982,599),(1005,610),(982,621)), fill=(233,251,251,230))
    for index, side in enumerate(("before", "after")):
        layer = blank()
        cx = centers[index]
        draw_label(layer, (cx-205, 535, cx+205, 665), row[side],
                   "#F7FBFF" if index == 0 else "#91F0DF", 104, 40)
        draw_label(layer, (cx-190, 680, cx+190, 723), row[f"{side}_en"],
                   "#D0ECEE", 25, 15)
        layers.append((f"row_1_{side}", layer))
    return base, layers


def layers_for_chart(chart, style):
    """Return separate base/content images keyed to reviewed speech cues."""
    if chart.get('layout', {}).get('mode') == 'fullscreen':
        if style.get("visual_theme") == "bilingual":
            return bilingual_fullscreen_comparison(chart, style)
        return fullscreen_comparison(chart, style)
    if style.get("visual_theme") == "bilingual":
        return bilingual_layers_for_chart(chart, style)
    if style.get("visual_theme") == "editorial":
        return editorial_layers_for_chart(chart, style)
    ink = style.get("text_color", "#111827")
    blue = style.get("brand_color", "#1685F8")
    accent = style.get("accent_color", "#FF8A18")
    panel = cp.hex_rgba(style.get("panel_color", "#FFFFFF"), 220)
    base, result = blank(), []
    d = ImageDraw.Draw(base)
    template = chart["template"]
    if template == "stat":
        d.rounded_rectangle((480, 90, 1440, 890), 42, fill=panel)
        draw_label(base, (540, 135, 1380, 240), chart["title"], ink, 62)
        draw_label(base, (550, 740, 1370, 835), chart.get("caption", ""), ink, 42)
        value = blank()
        text = str(chart["value"])
        if len(text) > 7:
            split = len(text) // 2
            draw_label(value, (780, 400, 1140, 500), text[:split], accent, 82)
            draw_label(value, (780, 500, 1140, 600), text[split:], accent, 82)
        else:
            draw_label(value, (780, 395, 1140, 600), text, accent, 112, 40)
        result.append(("value", value))
    elif template == "comparison":
        rows = chart["rows"]
        panel_bottom = 405 + (len(rows) - 1) * 145 + 120 + 70
        d.rounded_rectangle((120, 80, 1800, panel_bottom), 42, fill=panel)
        draw_label(base, (200, 120, 1720, 220), chart["title"], ink, 60)
        d.rounded_rectangle((180, 265, 1740, 370), 18, fill="white")
        draw_label(base, (715, 265, 1220, 370), chart["columns"][0], ink, 44)
        draw_label(base, (1220, 265, 1730, 370), chart["columns"][1], ink, 44)
        for i, row in enumerate(rows, 1):
            y = 405 + (i - 1) * 145
            d.rounded_rectangle((180, y, 1740, y + 120), 18, fill="white")
            draw_label(base, (190, y, 710, y + 120), row["label"], ink, 44)
            for side, box, color in [("before", (715, y, 1220, y + 120), blue), ("after", (1220, y, 1730, y + 120), accent)]:
                layer = blank()
                draw_label(layer, box, row[side], color, 68, 32)
                result.append((f"row_{i}_{side}", layer))
    else:
        left = 1000 if chart.get("placement") == "right" else 100
        right = left + 820
        count = len(chart["items"])
        height = min(125, (590 - (count - 1) * 44) // count)
        panel_bottom = 260 + (count - 1) * (height + 44) + height + 55
        d.rounded_rectangle((left, 70, right, panel_bottom), 42, fill=panel)
        draw_label(base, (left + 35, 110, right - 35, 230), chart["title"], ink, 56)
        for i, text in enumerate(chart["items"], 1):
            layer = blank()
            ld = ImageDraw.Draw(layer)
            y = 260 + (i - 1) * (height + 44)
            ld.rounded_rectangle((left + 50, y, right - 50, y + height), 20, fill=blue)
            draw_label(layer, (left + 65, y, right - 65, y + height), text, "white", 43, 27)
            if template == "flow" and i > 1:
                x = (left + right) // 2
                ld.line((x, y - 36, x, y - 14), fill=ink, width=6)
                ld.polygon([(x - 11, y - 18), (x + 11, y - 18), (x, y - 5)], fill=ink)
            result.append((f"item_{i}", layer))
    return base, result


STAT_RING_VARIANTS = ("halo", "ticks", "double", "orbit", "segments")


def fullscreen_comparison(chart, style):
    """Large before/after presentation; retain logos and subtitle safe areas."""
    base, layers = blank(), []
    d = ImageDraw.Draw(base)
    d.rectangle((0, 0, 1920, 1080), fill=(5, 15, 30, 105))
    blue = style.get('brand_color', '#1685F8')
    editorial_panel(base, (80, 155, 1840, 905), blue)
    draw_label(base, (190, 200, 1730, 300), chart['title'], '#FFFFFF', 82)
    rows = chart['rows']
    if not 1 <= len(rows) <= 2:
        raise ValueError('全屏动画对比每页最多两行 请拆分图表以保持字号与可读性')
    row_height = min(440, 520 / max(1, len(rows)))
    for i, row in enumerate(rows, 1):
        y = 335 + (i-1)*row_height
        draw_label(base, (220, y, 1700, y+60), row['label'], '#C4D9EE', 43)
        for side, x, fill, column in [('before', 220, (30,53,78,255), 0),
                                     ('after', 1070, (17,100,184,255), 1)]:
            draw_label(base, (x, y+65, x+630, y+120), chart['columns'][column], '#FFFFFF', 46)
            d.rounded_rectangle((x, y+140, x+630, y+row_height-15), 28, fill=fill,
                                outline=(130,190,240,220), width=3)
            layer = blank()
            draw_label(layer, (x+24,y+151,x+606,y+row_height-30), row[side],
                       '#FFFFFF' if side=='before' else '#FFCF91', 132, 38)
            layers.append((f'row_{i}_{side}', layer))
        cy = y+(140+row_height-15)/2
        d.line((907,cy,1000,cy),fill='#FFAB57',width=10)
        d.polygon([(986,cy-18),(1020,cy),(986,cy+18)], fill='#FFAB57')
    return base, layers


def ring_image(t, style=None, placement="left", variant="halo"):
    image = blank()
    d = ImageDraw.Draw(image)
    # Full decorative ring: no invented percentage implied for days/money.
    theme = (style or {}).get("visual_theme")
    editorial = theme in ("editorial", "bilingual")
    if editorial:
        ring_left = 1165 if placement == "right" else 265
        bounds = (ring_left, 250, ring_left + 490, 740)
    else:
        bounds = (715, 250, 1205, 740)
    progress = min(1, max(0, t / 1.2))
    progress = progress * progress * (3 - 2 * progress)
    if theme == "bilingual":
        blue, orange, faint = (200, 246, 241, 255), (255, 177, 164, 255), (200, 246, 241, 55)
    else:
        blue = (89, 190, 255, 255) if editorial else (22, 133, 248, 255)
        orange = (255, 159, 83, 255)
        faint = (155, 199, 233, 53) if editorial else (22, 133, 248, 30)
    cx = (bounds[0] + bounds[2]) / 2
    cy = (bounds[1] + bounds[3]) / 2

    def inset(pixels):
        return (bounds[0] + pixels, bounds[1] + pixels,
                bounds[2] - pixels, bounds[3] - pixels)

    if variant == "halo":
        d.ellipse(bounds, outline=faint, width=35)
        d.ellipse(inset(20), outline=(99, 168, 226, 70), width=3)
        if progress:
            d.arc(bounds, -90, -90 + 359.9 * progress, fill=blue, width=35)
    elif variant == "ticks":
        # Forty-eight luminous ticks make a clock-like frame, not a progress KPI.
        for index in range(48):
            angle = math.radians(index * 7.5 - 90)
            inner = 207 if index % 4 else 195
            outer = 244
            tone = blue if index < round(48 * progress) else faint
            d.line((cx + inner * math.cos(angle), cy + inner * math.sin(angle),
                    cx + outer * math.cos(angle), cy + outer * math.sin(angle)),
                   fill=tone, width=9 if index % 4 else 13)
        d.ellipse(inset(60), outline=(99, 168, 226, 110), width=4)
    elif variant == "double":
        d.ellipse(bounds, outline=faint, width=19)
        d.ellipse(inset(57), outline=faint, width=16)
        if progress:
            d.arc(bounds, -90, -90 + 359.9 * progress, fill=blue, width=19)
            d.arc(inset(57), 90, 90 + 359.9 * progress, fill=orange, width=16)
    elif variant == "orbit":
        d.ellipse(bounds, outline=faint, width=7)
        d.ellipse(inset(25), outline=(99, 168, 226, 72), width=4)
        for index, (start, span) in enumerate(((-90, 105), (45, 90), (175, 95))):
            if progress > index / 3:
                reveal = min(1, progress * 3 - index)
                d.arc(bounds, start, start + span * reveal,
                      fill=blue if index != 1 else orange, width=28)
        for angle, color in ((-90, blue), (45, orange), (175, blue)):
            theta = math.radians(angle)
            x, y = cx + 245 * math.cos(theta), cy + 245 * math.sin(theta)
            d.ellipse((x - 12, y - 12, x + 12, y + 12), fill=color)
    elif variant == "segments":
        for index in range(12):
            start = -90 + index * 30
            active = index < round(12 * progress)
            d.arc(bounds, start, start + 22,
                  fill=(orange if index % 3 == 0 else blue) if active else faint,
                  width=35)
        d.ellipse(inset(55), outline=(99, 168, 226, 100), width=5)
    else:
        raise ValueError(f"未知圆盘样式：{variant}")
    return image


def fade_keys(duration, move=True):
    ramp = min(0.28, duration / 4)
    keys = [{"property": "alpha", "time": t, "value": value}
            for t, value in [(0, 0), (ramp, 1), (duration - ramp, 1), (duration, 0)]]
    if move:
        keys += [{"property": "position_y", "time": 0, "value": -0.025},
                 {"property": "position_y", "time": min(0.36, duration / 3), "value": 0}]
    return keys


def encode_ring(path, seconds, fps, style=None, placement="left", variant="halo", transform=None):
    ffmpeg = find_ffmpeg()
    command = [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", "1920x1080",
               "-r", str(fps), "-i", "pipe:0", "-an", "-c:v", "prores_ks", "-profile:v", "4",
               "-pix_fmt", "yuva444p10le", "-alpha_bits", "16", "-threads", "4", str(path)]
    with (path.with_suffix(".log")).open("wb") as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
        try:
            for index in range(round(seconds * fps)):
                frame = ring_image(index / fps, style, placement, variant)
                process.stdin.write((transform(frame) if transform else frame).tobytes())
        finally:
            process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError(f"透明圆环生成失败：{path.with_suffix('.log')}")


def render_motion(work, plan_path=None):
    plan_path = plan_path or work / "chart_plan.json"
    plan = cp.load_json(plan_path)
    validate_synced(plan, work)
    if plan.get('layout_policy') == 'subject_safe':
        if (plan.get('layout_review_sha256') != cp.file_sha256(work/'chart_layout_review.json')
                or any(not c.get('layout') for c in plan['charts'])):
            raise ValueError('布局审核已改变或未完成 请重新 apply')
    if plan.get("canvas") != {"width": 1920, "height": 1080}:
        raise ValueError("当前动画模板要求 1920×1080 画布。")
    fingerprint = motion_fingerprint(plan)
    directory = work / "charts" / "motion" / fingerprint[:12]
    directory.mkdir(parents=True, exist_ok=True)
    fps, rendered, materials, posters = float(plan["motion"]["fps"]), [], [], []
    stat_ordinal = 0
    for chart in plan["charts"]:
        print(f"渲染动态图表 {chart['id']} {chart['title']}", flush=True)
        base, layers = layers_for_chart(chart, plan.get("style", {}))
        from chart_layout import transform_layer, render_blur
        source_box = base.getbbox()
        layout = chart.get('layout')
        transform = lambda im: transform_layer(im, source_box, layout)
        base = transform(base)
        layers = [(key, layer if key == "full_mask" else transform(layer)) for key, layer in layers]
        start, end = chart["timeline_in"], chart["timeline_out"]
        cues = {c["key"]: c for c in chart["synced_cues"]}
        records = []
        if layout and layout.get('blur'):
            background = render_blur(work, chart, directory, fps)
            records.append(background)
            materials.append({'path': background['source'], 'width':1920, 'height':1080,
                              'duration_seconds':background['source_out']})

        def save_layer(key, image, at, move=True):
            target = directory / f"{chart['id']}_{key}.png"
            image.save(target)
            records.append({"chart_id": chart["id"], "layer_key": key, "source": str(target.resolve()),
                            "timeline_in": at, "timeline_out": end,
                            "keyframes": fade_keys(end - at, False if layout else move), "title": chart["title"],
                            "z_group": 5 if key == 'full_mask' else (10 if key == 'base' else 30),
                            "template": chart["template"], "status": "approved", "section_ids": chart["section_ids"]})

        mask_layers = [(key, layer) for key, layer in layers if key == "full_mask"]
        content_layers = [(key, layer) for key, layer in layers if key != "full_mask"]
        for key, layer in mask_layers:
            save_layer(key, layer, start, False)
        save_layer("base", base, start, False)
        poster = blank()
        for _, layer in mask_layers:
            poster.alpha_composite(layer)
        poster.alpha_composite(base)
        if chart["template"] == "stat":
            variant = chart.get("ring_variant") or STAT_RING_VARIANTS[stat_ordinal % len(STAT_RING_VARIANTS)]
            stat_ordinal += 1
            ring_at = cues["value"]["reveal_at"]
            seconds = end - ring_at
            path = directory / f"{chart['id']}_ring.mov"
            encode_ring(path, seconds, fps, plan.get("style", {}), chart.get("placement", "left"), variant, transform)
            records.append({"chart_id": chart["id"], "layer_key": "ring", "source": str(path.resolve()),
                            "source_in": 0, "source_out": seconds, "timeline_in": ring_at, "timeline_out": end,
                            "keyframes": fade_keys(seconds, False), "title": chart["title"], "status": "approved",
                            "template": "stat", "z_group":20, "ring_variant": variant, "section_ids": chart["section_ids"]})
            materials.append({"path": str(path.resolve()), "width": 1920, "height": 1080, "duration_seconds": seconds})
            poster.alpha_composite(transform(ring_image(2, plan.get("style", {}), chart.get("placement", "left"), variant)))
        for key, layer in content_layers:
            save_layer(key, layer, cues[key]["reveal_at"])
            poster.alpha_composite(layer)
        target = directory / f"{chart['id']}_poster.png"
        poster.save(target)
        posters.append(target)
        rendered.append({"id": chart["id"], "source": str(target.resolve()), "layers": records})
    sheet = Image.new("RGB", (960, 270 * math.ceil(len(posters) / 2)), (55, 60, 70))
    for i, path in enumerate(posters):
        background = Image.new("RGBA", (1920, 1080), (65, 70, 82, 255))
        with Image.open(path) as overlay:
            background.alpha_composite(overlay)
        sheet.paste(background.convert("RGB").resize((480, 270)), (i % 2 * 480, i // 2 * 270))
    sheet_path = directory / "chart_contact_sheet.jpg"
    sheet.save(sheet_path, quality=92)
    manifest = {"schema_version": 2, "motion": True, "source_chart_plan": str(plan_path.resolve()),
                "chart_plan_fingerprint": fingerprint, "rendered": rendered, "materials": materials,
                "contact_sheet": str(sheet_path.resolve())}
    cp.write_json(work / "chart_render_manifest.json", manifest)
    return manifest


def attach_motion(work, base_plan_path=None):
    base_plan_path = base_plan_path or work / "edit_plan.json"
    plan, manifest = cp.load_json(work / "chart_plan.json"), cp.load_json(work / "chart_render_manifest.json")
    validate_synced(plan, work)
    cp.validate_chart_plan(plan, base_plan_path)
    if manifest.get("chart_plan_fingerprint") != motion_fingerprint(plan):
        raise ValueError("图表内容或时间在渲染后已改变，请重新 render。")
    graphics = [layer for chart in manifest["rendered"] for layer in chart["layers"]]
    for layer in graphics:
        if not Path(layer["source"]).is_file():
            raise FileNotFoundError(layer["source"])
    result = cp.load_json(base_plan_path)
    result.setdefault("tracks", {})["graphics"] = graphics
    result["chart_metrics"] = {"count": len(plan["charts"]), "layer_count": len(graphics),
                               "approved_count": len(plan["charts"]), "review_required": False,
                               "motion": True, "timing": "retained_asr_words",
                               "source_chart_plan": str((work / "chart_plan.json").resolve())}
    cp.write_json(work / "edit_plan_with_charts.json", result)
    inventory = cp.load_json(work / "media_inventory.json")
    inventory["graphics"] = manifest["materials"]
    cp.write_json(work / "media_inventory_with_charts.json", inventory)
    return result
