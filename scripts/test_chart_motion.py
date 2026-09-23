"""Focused checks for speech anchors and data-selection failure modes."""
import unittest
import json
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

import chart_motion as motion
import chart_pipeline as charts
import branding


class MotionTimingTests(unittest.TestCase):
    def test_motion_list_panel_fits_item_count(self):
        style = {"panel_color": "#FFFFFF"}
        two, _ = motion.layers_for_chart({"template": "list", "title": "优势", "items": ["甲", "乙"]}, style)
        four, _ = motion.layers_for_chart({"template": "list", "title": "优势", "items": ["甲", "乙", "丙", "丁"]}, style)
        self.assertGreater(two.getpixel((110, 550))[3], 0)
        self.assertEqual(two.getpixel((110, 700))[3], 0)
        self.assertGreater(four.getpixel((110, 700))[3], 0)

    def test_motion_comparison_panel_fits_row_count(self):
        style = {"panel_color": "#FFFFFF"}
        row = {"label": "周期", "before": "7天", "after": "5天"}
        chart = {"template": "comparison", "title": "方案周期", "columns": ["使用前", "使用后"]}
        one, _ = motion.layers_for_chart({**chart, "rows": [row]}, style)
        three, _ = motion.layers_for_chart({**chart, "rows": [row, row, row]}, style)
        self.assertGreater(one.getpixel((130, 500))[3], 0)
        self.assertEqual(one.getpixel((130, 700))[3], 0)
        self.assertGreater(three.getpixel((130, 700))[3], 0)

    def test_static_panels_also_fit_content(self):
        style = {"panel_color": "#FFFFFF", "brand_color": "#1685F8",
                 "accent_color": "#FF8A18", "text_color": "#111827"}
        short_list = Image.new("RGBA", (1920, 1080))
        charts.render_list({"title": "优势", "items": ["甲", "乙"]}, short_list, style)
        self.assertEqual(short_list.getpixel((90, 700))[3], 0)
        one_row = Image.new("RGBA", (1920, 1080))
        charts.render_comparison({"title": "周期", "rows": [
            {"label": "方案", "before": "7天", "after": "5天"}]}, one_row, style)
        self.assertEqual(one_row.getpixel((130, 700))[3], 0)

    def test_editorial_theme_preserves_layer_keys_and_safe_area(self):
        style = {"visual_theme": "editorial"}
        list_base, list_layers = motion.layers_for_chart(
            {"template": "list", "title": "使用分寸", "items": ["只展示关键角度", "不一次给出全案"]}, style)
        self.assertEqual([key for key, _ in list_layers], ["item_1", "item_2"])
        self.assertEqual(list_base.getpixel((110, 700))[3], 0)
        self.assertEqual(list_base.getpixel((110, 970))[3], 0)
        comparison_base, comparison_layers = motion.layers_for_chart(
            {"template": "comparison", "title": "方案周期", "columns": ["使用前", "使用后"],
             "rows": [{"label": "所需时间", "before": "7天", "after": "5天"}]}, style)
        self.assertEqual([key for key, _ in comparison_layers],
                         ["row_1_before", "row_1_after"])
        self.assertEqual(comparison_base.getpixel((160, 700))[3], 0)
        self.assertEqual(comparison_base.getpixel((160, 970))[3], 0)
        self.assertLessEqual(comparison_base.getbbox()[2], 955)
        stat_base, _ = motion.layers_for_chart(
            {"template": "stat", "title": "上手时间", "value": "两天", "caption": "完成基础上手"}, style)
        self.assertLessEqual(stat_base.getbbox()[2], 935)
        self.assertLessEqual(motion.ring_image(2, style).getbbox()[2], 756)

    def test_numeric_word_boundaries_keep_time(self):
        text, spans = motion.timed_characters([
            {"word": "十", "start": 4, "end": 4.2},
            {"word": "四", "start": 4.2, "end": 4.4},
            {"word": "万", "start": 4.4, "end": 4.8},
        ])
        self.assertEqual(text, "14万")
        self.assertEqual(spans[0], (4, 4.4))
        self.assertEqual(spans[-1], (4.4, 4.8))

    def test_trimmed_clip_maps_to_final_timeline(self):
        clip = {"source": "example.mp4", "source_in": 100, "source_out": 110,
                "timeline_in": 20, "timeline_out": 30}
        file = {"segments": [{"words": [
            {"word": "客单价", "start": 101, "end": 102},
            {"word": "十四万左右", "start": 102, "end": 104},
        ]}]}
        cue = motion.locate_cue({"key": "value", "quote": "14万左右", "section": 1},
                                {"text": "客单价是14万左右"}, clip, file)
        self.assertEqual(cue["timeline_in"], 22)
        self.assertEqual(cue["timeline_out"], 24)
        self.assertEqual(cue["word_coverage"], 1)

    def test_ambiguous_phrase_does_not_guess(self):
        with self.assertRaisesRegex(ValueError, "重复"):
            motion.locate_cue({"quote": "两天"}, {"text": "以前两天 现在两天"}, {}, {})

    def test_missing_speech_does_not_use_character_proportions(self):
        clip = {"source_in": 0, "source_out": 10, "timeline_in": 0, "timeline_out": 10}
        with self.assertRaisesRegex(ValueError, "对齐不足"):
            motion.locate_cue({"quote": "三分钟"}, {"text": "三分钟完成"}, clip, {"segments": []})

    def test_area_and_price_do_not_become_time_comparison(self):
        result = charts._comparison_items("80方户型 一个小时 传统需要一到两天",
                                          ["80方", "一个小时", "一到两天"])
        self.assertEqual(result["rows"][0]["before"], "一到两天")
        self.assertEqual(result["rows"][0]["after"], "一个小时")
        self.assertEqual(charts._comparison_items("客单价14万 传统一天", ["14万", "一天"]), {})

    def test_counter_is_not_a_kpi(self):
        self.assertEqual(charts.extract_metrics("完整输出一套方案需要一周"), ["一周"])

    def test_five_stat_rings_have_distinct_designs(self):
        style = {"visual_theme": "editorial"}
        images = [motion.ring_image(2, style, variant=name) for name in motion.STAT_RING_VARIANTS]
        self.assertEqual(len(images), 5)
        self.assertEqual(len({image.tobytes() for image in images}), 5)
        self.assertTrue(all(image.getbbox() for image in images))

    def test_bilingual_theme_masks_full_frame_and_separates_text_zones(self):
        chart = {"id":"chart_01","template":"stat","title":"九鼎装饰",
                 "title_en":"JIUDING DECORATION","value":"28年","value_en":"28 YEARS",
                 "caption":"以服务为导向","caption_en":"SERVICE FIRST",
                 "layout":{"mode":"center","blur":True}}
        base, layers = motion.layers_for_chart(chart, {"visual_theme":"bilingual"})
        by_key = dict(layers)
        self.assertGreater(by_key["full_mask"].getpixel((5, 5))[3], 0)
        self.assertGreater(by_key["full_mask"].getpixel((5, 1074))[3], 0)
        value_box = by_key["value"].getbbox()
        self.assertGreaterEqual(value_box[1], 420)
        self.assertLessEqual(value_box[3], 635)
        self.assertTrue(base.getbbox())

    def test_bilingual_comparison_uses_distinct_pair_rings_and_full_mask(self):
        chart={"id":"chart_07","template":"comparison","title":"前期意向方案周期",
               "title_en":"PRELIMINARY CONCEPT TURNAROUND",
               "columns":["使用前","使用后"],"columns_en":["BEFORE","AFTER"],
               "rows":[{"label":"所需时间","label_en":"TIME REQUIRED","before":"7天",
                        "before_en":"7 DAYS","after":"5天","after_en":"5 DAYS"}],
               "layout":{"mode":"fullscreen","blur":True}}
        base, layers = motion.layers_for_chart(chart,{"visual_theme":"bilingual"})
        self.assertGreater(base.getpixel((4,4))[3],0)
        self.assertGreater(base.getpixel((4,1075))[3],0)
        self.assertNotEqual(base.crop((330,350,850,860)).tobytes(),
                            base.crop((1070,350,1590,860)).tobytes())
        self.assertEqual([key for key,_ in layers],["row_1_before","row_1_after"])
        self.assertTrue(all(535 <= image.getbbox()[1] < image.getbbox()[3] <= 724
                            for _,image in layers))

    def test_editorial_before_value_has_opaque_background(self):
        chart = {"template": "comparison", "title": "出图时间", "columns": ["使用前", "使用后"],
                 "rows": [{"label": "所需时间", "before": "两天", "after": "一小时"}]}
        base, _ = motion.layers_for_chart(chart, {"visual_theme": "editorial"})
        self.assertEqual(base.getpixel((205, 390))[3], 255)

    def test_series_type_detection_is_explicit(self):
        self.assertEqual(branding.detect_case_type(Path("案例_AI智能设计平台")), "ai")
        self.assertEqual(branding.detect_case_type(Path("案例_生产对接")), "production")
        with self.assertRaises(ValueError):
            branding.detect_case_type(Path("未标记案例"))

    def test_branding_attach_keeps_chart_tracks(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            original = {"timeline": {"duration_seconds": 300}, "tracks": {"graphics": [{"chart_id": "chart_01"}]}}
            path = work / "edit_plan_with_charts.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            branding.attach_headers(work, {"case_type": "ai", "customer_logo": "customer.png",
                                          "layers": {"left": "left.png", "right": "right.png"}})
            updated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(updated["tracks"]["graphics"]), 3)
            self.assertEqual(updated["tracks"]["graphics"][0], {"chart_id": "chart_01"})
            self.assertEqual(updated["tracks"]["graphics"][-1]["timeline_out"], 300)
            with self.assertRaises(ValueError):
                branding.attach_headers(work, {"case_type": "ai", "customer_logo": "customer.png",
                                               "layers": {"left": "left.png", "right": "right.png"}})

    def test_white_customer_logo_background_is_removed(self):
        artwork = Image.new("RGBA", (60, 30), "white")
        ImageDraw.Draw(artwork).rectangle((15, 8, 45, 22), fill=(180, 30, 35, 255))
        result = branding._remove_connected_white(artwork)
        self.assertEqual(result.getpixel((0, 0))[3], 0)
        self.assertEqual(result.getpixel((30, 15))[3], 255)

    def test_ai_logo_layers_have_no_background_panels(self):
        with tempfile.TemporaryDirectory() as folder:
            customer = Path(folder) / "customer.jpg"
            image = Image.new("RGB", (180, 65), "white")
            ImageDraw.Draw(image).rectangle((10, 12, 160, 50), fill="#BC3030")
            image.save(customer)
            output = branding.render_headers("ai", customer, Path(folder) / "renders")
            pair = output["layout"]["pair"]
            ai_box = output["layout"]["ai_platform_bbox"]
            self.assertEqual(pair["kujiale_bbox"][3] - pair["kujiale_bbox"][1], branding.LOGO_HEIGHT)
            self.assertEqual(pair["customer_bbox"][3] - pair["customer_bbox"][1], branding.LOGO_HEIGHT)
            self.assertEqual(ai_box[1], branding.LOGO_MARGIN)
            self.assertEqual(branding.CANVAS[0] - ai_box[2], branding.LOGO_MARGIN)
            with Image.open(output["layers"]["left"]) as left, Image.open(output["layers"]["right"]) as right:
                self.assertEqual(left.getpixel((5, 30))[3], 0)
                self.assertEqual(left.getpixel((500, 40))[3], 0)
                self.assertEqual(right.getpixel((1300, 30))[3], 0)
                bar = pair["separator_bbox"]
                self.assertEqual(left.getpixel((bar[0], bar[1] + 5))[:3], (255, 255, 255))
                kj = left.crop(tuple(pair["kujiale_bbox"]))
                self.assertTrue(all(pixel[:3] == (255, 255, 255) for pixel in kj.getdata() if pixel[3] == 255))
                customer_art = left.crop(tuple(pair["customer_bbox"]))
                self.assertTrue(any(pixel[0] > pixel[1] for pixel in customer_art.getdata() if pixel[3] > 200))
                ai = right.crop(tuple(ai_box))
                self.assertTrue(any(pixel[:3] != (255, 255, 255) for pixel in ai.crop((0, 0, 65, ai.height)).getdata() if pixel[3] == 255))
                self.assertTrue(all(pixel[:3] == (255, 255, 255) for pixel in ai.crop((80, 0, ai.width, ai.height)).getdata() if pixel[3] == 255))
                self.assertIsNotNone(left.getbbox())
                self.assertIsNotNone(right.getbbox())
            production = branding.render_headers("production", customer, Path(folder) / "production")
            self.assertEqual(production["layout"]["pair"]["customer_bbox"][2],
                             branding.CANVAS[0] - branding.LOGO_MARGIN)
            with Image.open(production["layers"]["left"]) as left, Image.open(production["layers"]["right"]) as right:
                self.assertGreater(left.getpixel((40, 30))[3], 0)  # 系列文字条幅保留
                self.assertEqual(right.getpixel((1270, 30))[3], 0)  # Logo 无底板


if __name__ == "__main__":
    unittest.main()
