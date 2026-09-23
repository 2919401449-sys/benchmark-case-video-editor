"""Render and attach editable series corner branding to an existing chart edit plan.

Only the two generic product logos live in this skill. The customer's logo is
read from each case folder and copied into neither the skill nor source media.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CANVAS = (1920, 1080)
SERIES_TEXT = "设计生产一体化|定制标杆工厂系列"
ASSETS = Path(__file__).resolve().parents[1] / "assets" / "branding"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
LOGO_MARGIN = 24
LOGO_HEIGHT = round(46 * 1.20)  # 55 px; fixed baseline, never compound per run
PAIR_GAP = 18
# The fixed preset asset has a transparent gap between its colored AI mark and
# dark wordmark. Only pixels after that gap are turned white.
AI_WORDMARK_START = 350


def detect_case_type(project: Path, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    name = project.name.lower()
    is_ai = "ai智能设计平台" in name or "ai设计平台" in name
    is_production = "生产对接" in name
    if is_ai == is_production:
        raise ValueError("素材文件夹名须标注“AI智能设计平台”或“生产对接”；也可传 --case-type。")
    return "ai" if is_ai else "production"


def discover_customer_logo(project: Path, explicit: Path | None = None) -> Path:
    if explicit:
        path = explicit.resolve()
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise FileNotFoundError(f"客户 Logo 不存在或格式不支持：{path}")
        return path
    candidates = [p for p in project.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
    for dirname in ("logo", "Logo", "LOGO", "客户logo", "客户Logo"):
        folder = project / dirname
        if folder.is_dir():
            candidates.extend(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS)
    named = [p for p in candidates if "logo" in p.stem.lower() or "标识" in p.stem]
    choices = named or candidates
    if len(choices) != 1:
        raise ValueError(f"无法唯一确定客户 Logo（候选 {len(choices)} 个）；请使用 --customer-logo 指定。")
    return choices[0].resolve()


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc"):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _remove_connected_white(art: Image.Image) -> Image.Image:
    """Key out only a white exterior, leaving non-white artwork intact.

    Customer logos often arrive as JPEGs on white. Never alter the supplied
    file, and do not key an already transparent logo or a colored background.
    """
    if art.getchannel("A").getextrema()[0] < 255:
        return art
    corners = ((0, 0), (art.width - 1, 0), (0, art.height - 1),
               (art.width - 1, art.height - 1))
    if not all(min(art.getpixel(point)[:3]) >= 245 for point in corners):
        return art
    for point in corners:
        if min(art.getpixel(point)[:3]) >= 245:
            ImageDraw.floodfill(art, point, (0, 0, 0, 0), thresh=45)
    return art


def _load_logo(source: Path, crop=None) -> Image.Image:
    with Image.open(source) as original:
        art = original.convert("RGBA")
        if crop:
            left, top, right, bottom = crop
            if not (0 <= left < right <= art.width and 0 <= top < bottom <= art.height):
                raise ValueError("--customer-logo-crop 超出客户 Logo 图片范围。")
            art = art.crop((left, top, right, bottom))
        art = _remove_connected_white(art)
        bounds = art.getchannel("A").getbbox()
        if bounds is None:
            raise ValueError(f"Logo 没有可见像素：{source}")
        return art.crop(bounds)


def _fit_visible_height(art: Image.Image) -> Image.Image:
    width = max(1, round(art.width * LOGO_HEIGHT / art.height))
    return art.resize((width, LOGO_HEIGHT), Image.Resampling.LANCZOS)


def _white_wordmark(art: Image.Image) -> Image.Image:
    white = Image.new("RGBA", art.size, (255, 255, 255, 0))
    white.putalpha(art.getchannel("A"))
    return white


def _ai_logo_with_white_text(source: Path) -> Image.Image:
    art = _load_logo(source)
    if art.width <= AI_WORDMARK_START:
        raise ValueError("AI 平台预设 Logo 宽度异常，无法区分彩色图案和文字。")
    lettering = art.crop((AI_WORDMARK_START, 0, art.width, art.height))
    art.paste(_white_wordmark(lettering), (AI_WORDMARK_START, 0))
    return _fit_visible_height(art)


def _place_pair(layer: Image.Image, kujiale_logo: Path, customer_logo: Path,
                align: str, customer_crop=None) -> dict:
    kujiale = _fit_visible_height(_white_wordmark(_load_logo(kujiale_logo)))
    customer = _fit_visible_height(_load_logo(customer_logo, customer_crop))
    total_width = kujiale.width + customer.width + 2 * PAIR_GAP + 2
    if total_width > CANVAS[0] - 2 * LOGO_MARGIN:
        raise ValueError("品牌组合过宽，无法放入画面。")
    x = LOGO_MARGIN if align == "left" else CANVAS[0] - LOGO_MARGIN - total_width
    y = LOGO_MARGIN
    separator_x = x + kujiale.width + PAIR_GAP
    customer_x = separator_x + 2 + PAIR_GAP
    layer.alpha_composite(kujiale, (x, y))
    ImageDraw.Draw(layer).line((separator_x, y + 5, separator_x, y + LOGO_HEIGHT - 6),
                               fill=(255, 255, 255, 255), width=2)
    layer.alpha_composite(customer, (customer_x, y))
    return {"kujiale_bbox": [x, y, x + kujiale.width, y + LOGO_HEIGHT],
            "separator_bbox": [separator_x, y + 5, separator_x + 2, y + LOGO_HEIGHT - 5],
            "customer_bbox": [customer_x, y, customer_x + customer.width, y + LOGO_HEIGHT]}


def render_headers(case_type: str, customer_logo: Path, destination: Path,
                   customer_crop=None) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    kujiale = ASSETS / "kujiale.png"
    ai_platform = ASSETS / "ai-design-platform.png"
    for asset in (kujiale, ai_platform):
        if not asset.is_file():
            raise FileNotFoundError(f"预设品牌素材缺失：{asset}")
    left = Image.new("RGBA", CANVAS)
    right = Image.new("RGBA", CANVAS)
    if case_type == "ai":
        pair = _place_pair(left, kujiale, customer_logo, "left", customer_crop)
        ai_art = _ai_logo_with_white_text(ai_platform)
        ai_x = CANVAS[0] - LOGO_MARGIN - ai_art.width
        right.alpha_composite(ai_art, (ai_x, LOGO_MARGIN))
        layout = {"pair": pair,
                  "ai_platform_bbox": [ai_x, LOGO_MARGIN, CANVAS[0] - LOGO_MARGIN,
                                       LOGO_MARGIN + LOGO_HEIGHT]}
    elif case_type == "production":
        d = ImageDraw.Draw(left)
        d.rounded_rectangle((34, 8, 726, 72), radius=18, fill=(23, 111, 203, 232))
        d.text((62, 18), SERIES_TEXT, font=_font(34), fill=(255, 255, 255, 255))
        layout = {"pair": _place_pair(right, kujiale, customer_logo, "right", customer_crop)}
    else:
        raise ValueError(f"未知案例类型：{case_type}")
    paths = {}
    for name, artwork in (("left", left), ("right", right)):
        target = destination / f"{case_type}_{name}_header.png"
        artwork.save(target)
        paths[name] = str(target.resolve())
    return {"case_type": case_type, "series_text": SERIES_TEXT if case_type == "production" else "",
            "customer_logo": str(customer_logo), "customer_logo_sha256": hashlib.sha256(customer_logo.read_bytes()).hexdigest(),
            "preset_logos": [str(kujiale), str(ai_platform)], "layers": paths,
            "layout": layout}


def attach_headers(work: Path, manifest: dict) -> dict:
    plan_path = work / "edit_plan_with_charts.json"
    if not plan_path.is_file():
        raise FileNotFoundError("请先完成动态图表 attach，再接入系列角标。")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    graphics = plan.setdefault("tracks", {}).setdefault("graphics", [])
    if any(clip.get("template") == "series_branding" for clip in graphics):
        raise ValueError("该剪辑计划已接入系列角标；请勿重复接入。")
    duration = float(plan["timeline"]["duration_seconds"])
    if duration <= 0:
        raise ValueError("成片时长非法。")
    for side in ("left", "right"):
        graphics.append({"chart_id": "series_branding", "layer_key": side,
                         "source": manifest["layers"][side], "timeline_in": 0,
                         "timeline_out": duration, "title": "系列角标",
                         "template": "series_branding", "status": "approved"})
    plan["branding_metrics"] = {"case_type": manifest["case_type"], "layer_count": 2,
                                "duration_seconds": duration, "customer_logo": manifest["customer_logo"]}
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description="生成系列角标并接入剪映图表计划")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--case-type", choices=("ai", "production"))
    parser.add_argument("--customer-logo", type=Path)
    parser.add_argument("--customer-logo-crop", help="可选裁切：左,上,右,下（像素）")
    args = parser.parse_args()
    project, work = args.project.resolve(), args.work.resolve()
    case_type = detect_case_type(project, args.case_type)
    logo = discover_customer_logo(project, args.customer_logo)
    crop = tuple(int(part) for part in args.customer_logo_crop.split(",")) if args.customer_logo_crop else None
    if crop is not None and len(crop) != 4:
        parser.error("--customer-logo-crop 需要四个整数。")
    manifest = render_headers(case_type, logo, work / "branding", crop)
    attach_headers(work, manifest)
    (work / "branding" / "branding_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
