#!/usr/bin/env python3
"""Plan, render, and attach editable-timeline infographic overlays.

The renderer intentionally creates transparent 1920x1080 PNG overlays.  They
are lightweight, deterministic, and can be placed on a Jianying video track
without re-encoding the underlying A-roll or B-roll.  The generated graphic is
editable as one timeline item; its internal text and shapes are baked into the
PNG.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont


SCHEMA_VERSION = 1
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_COUNT = 7
SUPPORTED_TEMPLATES = {"stat", "comparison", "list", "flow"}

FONT_REGULAR_CANDIDATES = [
    Path(r"C:\Windows\Fonts\msyh.ttc"),
    Path(r"C:\Windows\Fonts\simhei.ttf"),
]
FONT_BOLD_CANDIDATES = [
    Path(r"C:\Windows\Fonts\msyhbd.ttc"),
    Path(r"C:\Windows\Fonts\simhei.ttf"),
]

DATA_PATTERN = re.compile(
    r"(?:\d+(?:\.\d+)?(?:\s*[-到至]\s*\d+(?:\.\d+)?)?"
    r"|[一二三四五六七八九十百千万两半]+(?:到|至)?[一二三四五六七八九十百千万两半]*)"
    r"\s*(?:个)?(?:%|％|年|周|天|分钟|小时|万|亿|人|套|方|平方米|单|元|种)",
    re.IGNORECASE,
)

PUNCTUATION_RE = re.compile(r"[，。；：！？、,.!?;:（）()【】\[\]“”‘’\"']+")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compact_for_evidence(text: str) -> str:
    return re.sub(r"\s+", "", clean_text(text))


def clean_text(text: str) -> str:
    text = PUNCTUATION_RE.sub(" ", str(text))
    return re.sub(r"\s+", " ", text).strip()


def compact_text(text: str, limit: int = 22) -> str:
    text = clean_text(text)
    text = re.sub(
        r"^(?:大家好|那|那么|所以|其实|然后|另外|我们认为|我觉得|就是|这个|的话)+\s*",
        "",
        text,
    )
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def extract_metrics(text: str) -> list[str]:
    found: list[str] = []
    for match in DATA_PATTERN.finditer(text):
        value = re.sub(r"\s+", "", match.group(0))
        if value == "一套":
            continue
        if value in ("一个人", "一人", "一天") and not any(k in text for k in ("耗时", "只用", "需要", "缩短", "定金")):
            continue
        if value not in found:
            found.append(value)
    return found


def split_clauses(text: str) -> list[str]:
    parts = re.split(
        r"[，。；：！？,;:!?]|(?:\s+)|(?:另外)|(?:同时)|(?:然后)|(?:所以)|(?:之后)|(?:再结合)",
        text,
    )
    clauses = [compact_text(part, 20) for part in parts]
    return [part for part in clauses if len(part) >= 4]


def infer_title(text: str, template: str) -> str:
    rules = [
        (("成立", "年"), "企业积淀"),
        (("培训", "上手"), "培训上手速度"),
        (("交付", "周期"), "交付效率提升"),
        (("压缩", "天"), "方案周期缩短"),
        (("客单价", "定金"), "成交案例"),
        (("小户型", "方"), "小户型出图效率"),
        (("效果图", "分钟"), "AI 出图效率"),
        (("对内", "客户"), "AI 应用价值"),
        (("设计师", "业务"), "协同工作流程"),
        (("预算", "效果图"), "效果图的业务价值"),
    ]
    for needles, title in rules:
        if all(needle in text for needle in needles):
            return title
    return {
        "stat": "关键数据",
        "comparison": "使用前后对比",
        "list": "核心价值",
        "flow": "工作流程",
    }[template]


def infer_stat_caption(text: str, metric: str) -> str:
    rules = [
        (("成立", "年"), "企业持续服务客户"),
        (("效果图", "分钟"), "生成高质量效果图"),
        (("培训", "上手"), "完成基础上手"),
        (("客单价", "万"), "AI 辅助成交客单价"),
        (("定金", "一天"), "从方案展示到意向成交"),
        (("户型", "方"), "适用刚需户型"),
    ]
    for needles, caption in rules:
        if all(needle in text for needle in needles):
            return caption
    without_metric = clean_text(text.replace(metric, ""))
    return compact_text(without_metric, 16) or "关键数据"


def _comparison_items(text: str, metrics: list[str]) -> dict[str, Any]:
    # Comparisons of time must not accidentally use a preceding area/price.
    time_metrics = [m for m in metrics if re.search(r"(?:周|天|小时|分钟)$", m)]
    if len(time_metrics) >= 2:
        metrics = time_metrics
    elif len(metrics) >= 2:
        # Other quantitative comparisons need a shared unit as well.
        units = [re.search(r"(?:%|％|万|亿|人|方|元)$", m) for m in metrics[:2]]
        if not all(units) or units[0].group() != units[1].group():
            return {}
    if len(metrics) < 2:
        return {}
    before, after = metrics[0], metrics[1]
    if "传统" in text and ("半小时" in text or "一个小时" in text):
        before, after = metrics[-1], metrics[0]
    label = "核心指标"
    if any(word in text for word in ("周期", "时间", "天", "小时", "分钟")):
        label = "所需时间"
    elif any(word in text for word in ("人员", "人")):
        label = "处理人员"
    elif any(word in text for word in ("利用率", "%", "％")):
        label = "利用率"
    return {
        "columns": ["使用前", "使用后"],
        "rows": [{"label": label, "before": before, "after": after}],
    }


def candidate_from_subtitle(item: dict[str, Any]) -> dict[str, Any] | None:
    text = str(item.get("text", ""))
    cleaned = clean_text(text)
    metrics = extract_metrics(text)
    comparison_cues = ("压缩到", "传统", "使用前", "使用后", "提升", "降低", "缩短", "减少", "增加")
    clauses = split_clauses(text)

    comparison = _comparison_items(text, metrics) if len(metrics) >= 2 else {}
    if comparison and any(cue in text for cue in comparison_cues):
        template = "comparison"
        content = comparison
        score = 8.0 + min(len(metrics), 3)
    elif metrics:
        template = "stat"
        content = {
            "value": metrics[0],
            "caption": infer_stat_caption(text, metrics[0]),
        }
        score = 6.0 + min(len(metrics), 3)
    elif len(clauses) >= 3 and sum(cue in text for cue in ("对内", "对客户", "后端", "第一", "第二", "第三")) >= 2:
        template = "list"
        content = {"items": clauses[:4]}
        score = 5.5
    elif len(clauses) >= 3 and any(
        cue in text for cue in ("再结合", "然后", "流程")
    ):
        template = "flow"
        content = {"items": clauses[:4]}
        score = 5.0
    else:
        return None

    duration = min(8.0, max(4.5, float(item["timeline_out"]) - float(item["timeline_in"]) - 0.6))
    center = (float(item["timeline_in"]) + float(item["timeline_out"])) / 2
    timeline_in = max(float(item["timeline_in"]) + 0.25, center - duration / 2)
    timeline_out = min(float(item["timeline_out"]) - 0.25, timeline_in + duration)
    if timeline_out - timeline_in < 3.5:
        return None

    if str(item.get("speaker", "")) in ("周总", "董事长", "总经理"):
        score += 0.5
    if any(weak in text for weak in ("隔了一天", "有效期", "从事", "大概百分之")):
        score -= 2.0
    if any(strong in text for strong in ("客单价", "基本上能上手", "高质量", "压缩到", "传统")):
        score += 1.5
    return {
        "candidate_id": f"section_{int(item['section']):03d}",
        "section_ids": [int(item["section"])],
        "speaker": item.get("speaker", ""),
        "template": template,
        "title": infer_title(text, template),
        "timeline_in": round(timeline_in, 3),
        "timeline_out": round(timeline_out, 3),
        "source_text": text,
        "metrics": metrics,
        "score": round(score, 2),
        "confidence": "needs_semantic_review",
        "review_required": True,
        **content,
    }


def proposals_from_edit_plan(edit_plan: dict[str, Any]) -> list[dict[str, Any]]:
    proposals = []
    for item in edit_plan.get("tracks", {}).get("subtitles", []):
        candidate = candidate_from_subtitle(item)
        if candidate is not None:
            proposals.append(candidate)
    return proposals


def motion_proposals(edit_plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Look across adjacent sentences so setup and result stay together."""
    subtitles = edit_plan.get("tracks", {}).get("subtitles", [])
    proposals = proposals_from_edit_plan(edit_plan)
    for index, first in enumerate(subtitles):
        for count in (2, 3):
            group = subtitles[index:index + count]
            if len(group) < count or any(s.get("speaker") != first.get("speaker") for s in group):
                continue
            if float(group[-1]["timeline_out"]) - float(first["timeline_in"]) > 32:
                continue
            merged = {**first, "text": " ".join(s["text"] for s in group), "timeline_out": group[-1]["timeline_out"]}
            candidate = candidate_from_subtitle(merged)
            if candidate and candidate["template"] in ("flow", "list", "comparison"):
                candidate["section_ids"] = [s["section"] for s in group]
                candidate["candidate_id"] += f"_through_{group[-1]['section']}"
                candidate["score"] -= 0.4 * (count - 1)
                proposals.append(candidate)
    # Spoken use constraints and benefits are useful graphics even without a KPI.
    for s in subtitles:
        if "只输出" in s["text"] and "不会" in s["text"]:
            proposals.append({"candidate_id": f"section_{s['section']}_constraints", "section_ids": [s["section"]],
                              "speaker": s.get("speaker", ""), "template": "list", "title": "前期谈单使用分寸",
                              "items": split_clauses(s["text"])[-2:], "source_text": s["text"],
                              "timeline_in": s["timeline_in"], "timeline_out": s["timeline_out"],
                              "score": 6.5, "review_required": True, "confidence": "needs_semantic_review"})
    for p in proposals:
        if any(word in p["source_text"] for word in ("从事", "隔了一天", "第二天过来")):
            p["score"] -= 5
        p["selection_reason"] = "优先明确数据与前后对比，其次具体流程和使用要点；按时间分散，逐项核对语义。"
    return proposals


def select_spread_candidates(
    proposals: list[dict[str, Any]],
    count: int,
    min_gap_seconds: float,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for candidate in sorted(proposals, key=lambda item: (-float(item["score"]), item["timeline_in"])):
        center = (float(candidate["timeline_in"]) + float(candidate["timeline_out"])) / 2
        if all(
            abs(center - (float(other["timeline_in"]) + float(other["timeline_out"])) / 2)
            >= min_gap_seconds
            for other in selected
        ):
            selected.append(candidate)
        if len(selected) >= count:
            break
    return sorted(selected, key=lambda item: float(item["timeline_in"]))


def propose_charts(work: Path, count: int, min_gap_seconds: float, motion: bool = False) -> dict[str, Any]:
    edit_plan_path = work / "edit_plan.json"
    if not edit_plan_path.is_file():
        raise FileNotFoundError("需要先生成 edit_plan.json。")
    edit_plan = load_json(edit_plan_path)
    proposals = motion_proposals(edit_plan) if motion else proposals_from_edit_plan(edit_plan)
    selected = select_spread_candidates(proposals, count, min_gap_seconds)
    charts = []
    for index, candidate in enumerate(selected, start=1):
        chart = dict(candidate)
        chart.pop("candidate_id", None)
        chart.pop("score", None)
        chart["id"] = f"chart_{index:02d}"
        chart["placement"] = "center" if chart["template"] in ("comparison", "stat") else "left"
        chart["status"] = "auto_proposed"
        if motion:
            from chart_motion import suggested_cues
            chart["cues"] = suggested_cues(chart, {int(s["section"]): s for s in edit_plan["tracks"]["subtitles"]})
        charts.append(chart)
    result = {
        "schema_version": SCHEMA_VERSION,
        "source_edit_plan": str(edit_plan_path.resolve()),
        "source_edit_plan_sha256": file_sha256(edit_plan_path),
        "canvas": {
            "width": int(edit_plan.get("timeline", {}).get("width", DEFAULT_WIDTH)),
            "height": int(edit_plan.get("timeline", {}).get("height", DEFAULT_HEIGHT)),
        },
        "style": {
            "brand_color": "#1685F8",
            "accent_color": "#FF8A18",
            "panel_color": "#FFFFFF",
            "text_color": "#111827",
        },
        "charts": charts,
        "review_required": True,
        "review_note": "自动提案必须核对文字、数字、使用前后方向和出现时间；确认后把 status 改为 approved。",
        "motion": {"enabled": motion},
    }
    write_json(work / "chart_candidates.json", {"schema_version": 1, "candidates": proposals})
    write_json(work / "chart_plan.json", result)
    return result


def _font_path(bold: bool) -> Path:
    candidates = FONT_BOLD_CANDIDATES if bold else FONT_REGULAR_CANDIDATES
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("找不到可用的中文字体（微软雅黑或黑体）。")


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_font_path(bold)), size=size)


def hex_rgba(value: str, alpha: int = 255) -> tuple[int, int, int, int]:
    value = value.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"颜色必须是 #RRGGBB：{value}")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4)) + (alpha,)


def fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, start: int, minimum: int, bold: bool = False):
    for size in range(start, minimum - 1, -2):
        candidate = font(size, bold)
        if draw.textbbox((0, 0), text, font=candidate)[2] <= max_width:
            return candidate
    return font(minimum, bold)


def draw_centered(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, used_font, fill) -> None:
    left, top, right, bottom = box
    bounds = draw.textbbox((0, 0), text, font=used_font)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    draw.text(
        (left + (right - left - width) / 2, top + (bottom - top - height) / 2 - bounds[1]),
        text,
        font=used_font,
        fill=fill,
    )


def draw_title(draw: ImageDraw.ImageDraw, title: str, x: int, y: int, max_width: int, color) -> None:
    title_font = fit_font(draw, title, max_width, 64, 42, bold=True)
    draw.text((x, y), title, font=title_font, fill=color)


def render_stat(chart: dict[str, Any], canvas: Image.Image, style: dict[str, Any]) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    text_color = hex_rgba(style["text_color"])
    accent = hex_rgba(style["accent_color"])
    panel = hex_rgba(style["panel_color"], 225)
    box = (390, 120, 1530, 850)
    draw.rounded_rectangle(box, radius=42, fill=panel)
    draw_title(draw, str(chart["title"]), 510, 185, 900, text_color)
    ring_box = (610, 330, 1010, 730)
    draw.ellipse(ring_box, outline=hex_rgba(style["accent_color"], 55), width=56)
    draw.arc(ring_box, start=-90, end=220, fill=accent, width=56)
    value = str(chart.get("value") or (chart.get("metrics") or ["—"])[0])
    # Keep a generous inset inside the ring.  Chinese glyph bearings and long
    # values such as "100方以内" otherwise touch or cross the inner edge.
    value_font = fit_font(draw, value, 275, 112, 54, bold=True)
    if draw.textbbox((0, 0), value, font=value_font)[2] <= 275:
        draw_centered(draw, ring_box, value, value_font, accent)
    else:
        # A long label is still a valid fact, but forcing it onto one line
        # makes it cross the ring. Keep both lines inside the inner circle.
        midpoint = max(2, len(value) // 2)
        lines = (value[:midpoint], value[midpoint:])
        for index, line in enumerate(lines):
            line_font = fit_font(draw, line, 270, 74, 38, bold=True)
            if draw.textbbox((0, 0), line, font=line_font)[2] > 270:
                raise ValueError(f"图表数值过长，无法放入圆环：{value}")
            draw_centered(
                draw,
                (ring_box[0], 450 + index * 90, ring_box[2], 540 + index * 90),
                line,
                line_font,
                accent,
            )
    caption = str(chart.get("caption", "关键数据"))
    caption_font = fit_font(draw, caption, 350, 44, 30)
    draw_centered(draw, (1050, 430, 1420, 650), caption, caption_font, text_color)


def render_list(chart: dict[str, Any], canvas: Image.Image, style: dict[str, Any]) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    text_color = hex_rgba(style["text_color"])
    brand = hex_rgba(style["brand_color"])
    panel = hex_rgba(style["panel_color"], 220)
    placement = chart.get("placement", "left")
    left = 80 if placement == "left" else 960
    right = left + 820
    items = [str(item) for item in chart.get("items", [])][:4]
    if not items:
        items = ["请在 chart_plan.json 中补充要点"]
    top = 260
    gap = 125
    panel_bottom = top + (len(items) - 1) * gap + 82 + 55
    draw.rounded_rectangle((left, 70, right, panel_bottom), radius=42, fill=panel)
    draw_title(draw, str(chart["title"]), left + 70, 125, 680, text_color)
    for index, item in enumerate(items, start=1):
        y = top + (index - 1) * gap
        draw.rounded_rectangle((left + 70, y, right - 70, y + 82), radius=20, fill=brand)
        label = f"{index:02d}  {item}"
        item_font = fit_font(draw, label, 610, 42, 28, bold=True)
        draw_centered(draw, (left + 90, y, right - 90, y + 82), label, item_font, (255, 255, 255, 255))


def render_flow(chart: dict[str, Any], canvas: Image.Image, style: dict[str, Any]) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    text_color = hex_rgba(style["text_color"])
    brand = hex_rgba(style["brand_color"])
    panel = hex_rgba(style["panel_color"], 220)
    placement = chart.get("placement", "right")
    left = 1040 if placement == "right" else 80
    right = left + 800
    items = [str(item) for item in chart.get("items", [])][:5]
    if not items:
        items = ["步骤一", "步骤二", "步骤三"]
    top = 245
    available = 585
    box_height = min(92, int((available - 46 * (len(items) - 1)) / max(1, len(items))))
    step = box_height + 46
    panel_bottom = top + (len(items) - 1) * step + box_height + 55
    draw.rounded_rectangle((left, 65, right, panel_bottom), radius=42, fill=panel)
    draw_title(draw, str(chart["title"]), left + 65, 110, 670, text_color)
    for index, item in enumerate(items):
        y = top + index * step
        draw.rounded_rectangle((left + 95, y, right - 95, y + box_height), radius=20, fill=brand)
        item_font = fit_font(draw, item, 570, 40, 27, bold=True)
        draw_centered(draw, (left + 110, y, right - 110, y + box_height), item, item_font, (255, 255, 255, 255))
        if index < len(items) - 1:
            x = (left + right) // 2
            draw.line((x, y + box_height + 6, x, y + box_height + 34), fill=text_color, width=7)
            draw.polygon([(x - 12, y + box_height + 28), (x + 12, y + box_height + 28), (x, y + box_height + 42)], fill=text_color)


def render_comparison(chart: dict[str, Any], canvas: Image.Image, style: dict[str, Any]) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    text_color = hex_rgba(style["text_color"])
    brand = hex_rgba(style["brand_color"])
    accent = hex_rgba(style["accent_color"])
    panel = hex_rgba(style["panel_color"], 230)
    columns = chart.get("columns", ["使用前", "使用后"])
    rows = chart.get("rows", [])
    if not rows:
        metrics = chart.get("metrics", ["—", "—"])
        rows = [{"label": "核心指标", "before": metrics[0], "after": metrics[1] if len(metrics) > 1 else "—"}]
    panel_bottom = 385 + (min(len(rows), 3) - 1) * 170 + 135 + 65
    draw.rounded_rectangle((120, 70, 1800, panel_bottom), radius=42, fill=panel)
    title = str(chart["title"])
    title_font = fit_font(draw, title, 1450, 64, 42, bold=True)
    draw_centered(draw, (220, 105, 1700, 200), title, title_font, text_color)
    col_x = [180, 700, 1210, 1720]
    header_y = 245
    draw.rounded_rectangle((180, header_y, 1720, header_y + 110), radius=18, fill=(255, 255, 255, 245))
    header_font = font(48, True)
    draw_centered(draw, (col_x[1], header_y, col_x[2], header_y + 110), str(columns[0]), header_font, text_color)
    draw_centered(draw, (col_x[2], header_y, col_x[3], header_y + 110), str(columns[1]), header_font, text_color)
    for index, row in enumerate(rows[:3]):
        y = 385 + index * 170
        draw.rounded_rectangle((180, y, 1720, y + 135), radius=18, fill=(255, 255, 255, 245))
        label_font = fit_font(draw, str(row.get("label", "指标")), 440, 46, 30)
        draw_centered(draw, (col_x[0] + 20, y, col_x[1] - 20, y + 135), str(row.get("label", "指标")), label_font, text_color)
        before_font = fit_font(draw, str(row.get("before", "—")), 430, 68, 40, bold=True)
        after_font = fit_font(draw, str(row.get("after", "—")), 430, 68, 40, bold=True)
        draw_centered(draw, (col_x[1], y, col_x[2], y + 135), str(row.get("before", "—")), before_font, brand)
        draw_centered(draw, (col_x[2], y, col_x[3], y + 135), str(row.get("after", "—")), after_font, accent)


def validate_chart_plan(
    plan: dict[str, Any],
    edit_plan_path: Path | None = None,
) -> None:
    charts = plan.get("charts", [])
    if not 1 <= len(charts) <= 20:
        raise ValueError("chart_plan.json 必须包含 1 至 20 个图表。")
    ids: set[str] = set()
    previous_out = -1.0
    for chart in sorted(charts, key=lambda item: float(item["timeline_in"])):
        chart_id = str(chart.get("id", ""))
        if not chart_id or chart_id in ids:
            raise ValueError(f"图表 id 缺失或重复：{chart_id}")
        ids.add(chart_id)
        if chart.get("template") not in SUPPORTED_TEMPLATES:
            raise ValueError(f"不支持的模板：{chart.get('template')}")
        template = chart["template"]
        if template in ("list", "flow") and not 2 <= len(chart.get("items", [])) <= (4 if template == "list" else 5):
            raise ValueError(f"{chart_id} 的列表需 2–4 项，流程需 2–5 项。")
        if template == "comparison" and (len(chart.get("columns", [])) != 2 or not 1 <= len(chart.get("rows", [])) <= 3):
            raise ValueError(f"{chart_id} 对比图需两列及 1–3 行。")
        timeline_in = float(chart["timeline_in"])
        timeline_out = float(chart["timeline_out"])
        if timeline_in < 0 or timeline_out <= timeline_in:
            raise ValueError(f"非法图表时间范围：{chart_id}")
        if timeline_in < previous_out - 0.001:
            raise ValueError(f"图表时间相互重叠：{chart_id}")
        previous_out = timeline_out
    if edit_plan_path is None:
        return
    if Path(str(plan.get("source_edit_plan", ""))).resolve() != edit_plan_path.resolve():
        raise ValueError("图表计划引用的剪辑计划路径已过期，请重新提案和审核。")
    expected_hash = str(plan.get("source_edit_plan_sha256") or "")
    if not expected_hash or expected_hash != file_sha256(edit_plan_path):
        raise ValueError("图表计划对应的剪辑内容已变化，请重新提案和审核。")
    edit_plan = load_json(edit_plan_path)
    subtitles = {
        int(item["section"]): item
        for item in edit_plan.get("tracks", {}).get("subtitles", [])
    }
    timeline_duration = float(edit_plan.get("timeline", {}).get("duration_seconds", 0.0))
    for chart in charts:
        chart_id = str(chart["id"])
        sections = [int(value) for value in chart.get("section_ids", [])]
        if not sections or any(section not in subtitles for section in sections):
            raise ValueError(f"{chart_id} 缺少当前字幕句段来源。")
        evidence = " ".join(str(subtitles[section]["text"]) for section in sections)
        if compact_for_evidence(str(chart.get("source_text", ""))) != compact_for_evidence(evidence):
            raise ValueError(f"{chart_id} 的 source_text 与当前字幕原话不一致。")
        source_start = min(float(subtitles[section]["timeline_in"]) for section in sections)
        source_end = max(float(subtitles[section]["timeline_out"]) for section in sections)
        chart_start = float(chart["timeline_in"])
        chart_end = float(chart["timeline_out"])
        if chart_start < source_start - 0.25 or chart_end > source_end + 0.25:
            raise ValueError(f"{chart_id} 出现时间不在对应口播句段内。")
        if chart_end > timeline_duration + 0.001:
            raise ValueError(f"{chart_id} 超出成片时长。")
        if chart.get("status") != "approved":
            raise ValueError(f"{chart_id} 尚未人工或 AI 逐项审核。")
        claims: list[str] = []
        if chart["template"] == "stat":
            claims.append(str(chart.get("value", "")))
        elif chart["template"] == "comparison":
            for row in chart.get("rows", []):
                claims.extend((str(row.get("before", "")), str(row.get("after", ""))))
        for claim in claims:
            if not claim or compact_for_evidence(claim) not in compact_for_evidence(evidence):
                raise ValueError(f"{chart_id} 的数值“{claim}”无法在对应口播中找到。")


def render_charts(work: Path, plan_path: Path | None = None) -> dict[str, Any]:
    plan_path = plan_path or (work / "chart_plan.json")
    plan = load_json(plan_path)
    if plan.get("motion", {}).get("enabled"):
        from chart_motion import render_motion
        return render_motion(work, plan_path)
    validate_chart_plan(plan, work / "edit_plan.json")
    width = int(plan.get("canvas", {}).get("width", DEFAULT_WIDTH))
    height = int(plan.get("canvas", {}).get("height", DEFAULT_HEIGHT))
    style = plan.get("style", {})
    style = {
        "brand_color": style.get("brand_color", "#1685F8"),
        "accent_color": style.get("accent_color", "#FF8A18"),
        "panel_color": style.get("panel_color", "#FFFFFF"),
        "text_color": style.get("text_color", "#111827"),
    }
    output_dir = work / "charts" / "rendered"
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered = []
    renderers = {
        "stat": render_stat,
        "comparison": render_comparison,
        "list": render_list,
        "flow": render_flow,
    }
    for chart in plan["charts"]:
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        renderers[chart["template"]](chart, canvas, style)
        target = output_dir / f"{chart['id']}.png"
        canvas.save(target, format="PNG", optimize=True)
        rendered.append(
            {
                "id": chart["id"],
                "source": str(target.resolve()),
                "template": chart["template"],
                "timeline_in": float(chart["timeline_in"]),
                "timeline_out": float(chart["timeline_out"]),
                "title": chart["title"],
                "status": chart.get("status", "unknown"),
            }
        )

    thumb_width = 480
    thumb_height = round(height * thumb_width / width)
    columns = 2
    rows = math.ceil(len(rendered) / columns)
    sheet = Image.new("RGB", (thumb_width * columns, thumb_height * rows), (36, 41, 48))
    for index, item in enumerate(rendered):
        image = Image.open(item["source"]).convert("RGBA")
        checker = Image.new("RGBA", image.size, (95, 101, 112, 255))
        checker.alpha_composite(image)
        checker.thumbnail((thumb_width, thumb_height))
        x = (index % columns) * thumb_width
        y = (index // columns) * thumb_height
        sheet.paste(checker.convert("RGB"), (x, y))
    contact_sheet = work / "charts" / "chart_contact_sheet.jpg"
    contact_sheet.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(contact_sheet, quality=90)

    manifest = {
        "schema_version": 1,
        "source_chart_plan": str(plan_path.resolve()),
        "rendered": rendered,
        "contact_sheet": str(contact_sheet.resolve()),
        "limitations": [
            "每张图在剪映中是一个可移动、缩放和改时长的透明图片片段。",
            "图片内部的文字和形状不是剪映原生元素，不能逐项编辑。",
        ],
    }
    write_json(work / "chart_render_manifest.json", manifest)
    return manifest


def attach_charts(work: Path, base_plan_path: Path | None = None) -> dict[str, Any]:
    base_plan_path = base_plan_path or (work / "edit_plan.json")
    base_plan = load_json(base_plan_path)
    chart_plan = load_json(work / "chart_plan.json")
    if chart_plan.get("motion", {}).get("enabled"):
        from chart_motion import attach_motion
        return attach_motion(work, base_plan_path)
    manifest = load_json(work / "chart_render_manifest.json")
    validate_chart_plan(chart_plan, base_plan_path)
    rendered_by_id = {item["id"]: item for item in manifest["rendered"]}
    graphics = []
    duration = float(base_plan.get("timeline", {}).get("duration_seconds", 0.0))
    for chart in chart_plan["charts"]:
        rendered = rendered_by_id.get(chart["id"])
        if rendered is None or not Path(rendered["source"]).is_file():
            raise FileNotFoundError(f"缺少图表渲染文件：{chart['id']}")
        timeline_in = float(chart["timeline_in"])
        timeline_out = min(float(chart["timeline_out"]), duration)
        if timeline_in >= duration or timeline_out <= timeline_in:
            raise ValueError(f"图表超出成片时间线：{chart['id']}")
        graphics.append(
            {
                "chart_id": chart["id"],
                "source": rendered["source"],
                "timeline_in": timeline_in,
                "timeline_out": timeline_out,
                "template": chart["template"],
                "title": chart["title"],
                "section_ids": chart.get("section_ids", []),
                "status": chart.get("status", "unknown"),
            }
        )
    result = json.loads(json.dumps(base_plan, ensure_ascii=False))
    result["schema_version"] = max(3, int(result.get("schema_version", 1)))
    result.setdefault("tracks", {})["graphics"] = graphics
    result["chart_metrics"] = {
        "count": len(graphics),
        "approved_count": sum(1 for item in graphics if item.get("status") == "approved"),
        "review_required": any(item.get("status") != "approved" for item in graphics),
        "source_chart_plan": str((work / "chart_plan.json").resolve()),
    }
    result["review_required"] = bool(result.get("review_required")) or result["chart_metrics"]["review_required"]
    output = work / "edit_plan_with_charts.json"
    write_json(output, result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="标杆案例图表规划与渲染")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--work", type=Path, required=True, help="剪辑工作目录")

    propose = subparsers.add_parser("propose", help="从字幕自动提出 5 至 10 个图表候选")
    common(propose)
    propose.add_argument("--count", type=int, default=DEFAULT_COUNT)
    propose.add_argument("--min-gap-seconds", type=float, default=22.0)
    propose.add_argument("--motion", action="store_true", help="生成动态图表提案与逐项口播锚点")

    sync = subparsers.add_parser("sync", help="把已审核图表的逐项锚点对齐到 A-roll 原始逐词时间")
    common(sync)

    render = subparsers.add_parser("render", help="把 chart_plan.json 渲染为透明 PNG")
    common(render)
    render.add_argument("--plan", type=Path)

    attach = subparsers.add_parser("attach", help="把图表轨加入新的剪辑计划")
    common(attach)
    attach.add_argument("--base-plan", type=Path)

    all_cmd = subparsers.add_parser("all", help="自动提案后停在审核关口；不会直接渲染")
    common(all_cmd)
    all_cmd.add_argument("--count", type=int, default=DEFAULT_COUNT)
    all_cmd.add_argument("--min-gap-seconds", type=float, default=22.0)
    all_cmd.add_argument("--motion", action="store_true")
    return parser


def print_summary(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    if args.command == "propose":
        result = propose_charts(work, args.count, args.min_gap_seconds, args.motion)
        print_summary({"charts": len(result["charts"]), "plan": str(work / "chart_plan.json")})
    elif args.command == "sync":
        from chart_motion import sync_charts
        print_summary(sync_charts(work))
    elif args.command == "render":
        result = render_charts(work, args.plan.resolve() if args.plan else None)
        print_summary({"charts": len(result["rendered"]), "contact_sheet": result["contact_sheet"]})
    elif args.command == "attach":
        result = attach_charts(work, args.base_plan.resolve() if args.base_plan else None)
        print_summary({"graphics": len(result["tracks"]["graphics"]), "plan": str(work / "edit_plan_with_charts.json")})
    elif args.command == "all":
        plan = propose_charts(work, args.count, args.min_gap_seconds, args.motion)
        print_summary(
            {
                "charts": len(plan["charts"]),
                "plan": str(work / "chart_plan.json"),
                "review_required": True,
                "next_step": "逐项核对图表事实、语义与时间，确认后将每项 status 改为 approved，再运行 render 和 attach。",
            }
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"错误：{exc}")
        raise SystemExit(1)
