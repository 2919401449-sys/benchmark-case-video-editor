from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import case_runner as runner


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class ProductRunnerTests(unittest.TestCase):
    def test_transcript_must_cover_every_inventory_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            one, two = work / "one.mp4", work / "two.mp4"
            write_json(work / "media_inventory.json", {"a_roll": [{"path": str(one)}, {"path": str(two)}]})
            write_json(work / "transcript.json", {"files": [{"source": str(one), "segments": [{"text": "a"}]}]})
            self.assertFalse(runner.transcript_complete(work))
            write_json(work / "transcript.json", {"files": [
                {"source": str(one), "segments": [{"text": "a"}]},
                {"source": str(two), "segments": [{"text": "b"}]},
            ]})
            self.assertTrue(runner.transcript_complete(work))

    def test_chart_gate_requires_explicit_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = Path(directory) / "chart_plan.json"
            write_json(plan, {"review_required": True, "charts": [{"status": "candidate"}]})
            self.assertFalse(runner.chart_approved(plan))
            write_json(plan, {"review_required": False, "charts": [{"status": "approved"}]})
            self.assertTrue(runner.chart_approved(plan))

    def test_layout_gate_requires_boxes_for_every_sample(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            review = Path(directory) / "chart_layout_review.json"
            write_json(review, {"charts": [{"status": "approved", "samples": [{"protected_boxes": None}]}]})
            self.assertFalse(runner.layout_approved(review))
            write_json(review, {"charts": [{"status": "approved", "samples": [{"protected_boxes": []}]}]})
            self.assertTrue(runner.layout_approved(review))

    def test_compose_gate_rejects_unbalanced_broll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            config = {"work": str(work), "case_type": "ai"}
            write_json(work / "edit_plan.json", {
                "tracks": {},
                "b_roll_metrics": {
                    "distribution_balanced": False,
                    "source_use_limit_respected": True,
                    "common_ai_quota": {"used": 5},
                },
            })
            self.assertFalse(runner.output_complete("compose", config))
            value = json.loads((work / "edit_plan.json").read_text(encoding="utf-8"))
            value["b_roll_metrics"]["distribution_balanced"] = True
            write_json(work / "edit_plan.json", value)
            self.assertTrue(runner.output_complete("compose", config))

    def test_presentation_gate_rejects_stale_edit_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            edit = work / "edit_plan.json"
            edit.write_text("{}", encoding="utf-8")
            notes = work / "presentation_notes.json"
            card = {"kind": "speaker", "status": "approved", "evidence": "V0"}
            write_json(notes, {"source_edit_plan_sha256": "stale", "cards": [card]})
            self.assertFalse(runner.presentation_approved(notes, edit))
            write_json(notes, {"source_edit_plan_sha256": runner.file_sha256(edit), "cards": [card]})
            self.assertTrue(runner.presentation_approved(notes, edit))

    def test_start_runs_strict_preflight_and_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, work = root / "project", root / "work"
            (project / "A-roll").mkdir(parents=True)
            (project / "A-roll" / "sample.mp4").write_bytes(b"fixture")
            args = SimpleNamespace(
                project=project,
                work=work,
                draft_root=None,
                jianying_install=None,
                draft_name="smoke",
                model=None,
                speaker_map=None,
                corrections=None,
                broll_labels=None,
                chart_count=0,
                skip_branding=True,
                skip_speaker_labels=True,
                skip_export=True,
                replace_state=False,
            )
            state = runner.create_state(args)
            self.assertEqual(runner.resume(state, stop_after="preflight"), 0)
            saved = runner.load_state(work)
            self.assertEqual(saved["stages"]["preflight"]["status"], "passed")
            self.assertTrue((work / "environment_report.json").is_file())


if __name__ == "__main__":
    unittest.main()
