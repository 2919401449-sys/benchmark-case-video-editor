#!/usr/bin/env python3
"""Checkpointed product runner for benchmark-case-video-editor.

The runner deliberately stops at semantic review gates. Codex reviews the
compact worksheet, writes the requested JSON file, and resumes the same run.
All deterministic commands use this one interpreter and write verbose output
to per-stage logs instead of flooding the model context.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
STATE_FILE = "run_state.json"
NEXT_ACTION_FILE = "NEXT_ACTION.md"
MAX_ATTEMPTS = 2

STAGES = [
    "preflight",
    "inspect",
    "thumbnails",
    "analyze_broll",
    "transcribe",
    "speaker_review",
    "build_v1",
    "prepare_v1_correction",
    "v1_review",
    "correct_v1",
    "v2_review",
    "shorten_v2",
    "align",
    "contact_sheets",
    "broll_review",
    "apply_broll_labels",
    "match_broll",
    "compose",
    "editorial_audit",
    "chart_propose",
    "chart_review",
    "chart_sync",
    "chart_layout_prepare",
    "chart_layout_review",
    "chart_layout_apply",
    "chart_render",
    "chart_attach",
    "branding",
    "speaker_label_review",
    "speaker_labels",
    "export_plain",
    "export_encrypted",
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def state_path(work: Path) -> Path:
    return work / STATE_FILE


def load_state(work: Path) -> dict[str, Any]:
    path = state_path(work)
    if not path.is_file():
        raise FileNotFoundError(f"没有运行状态：{path}。请先执行 start。")
    return read_json(path)


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = now()
    write_json(Path(state["config"]["work"]) / STATE_FILE, state)


def json_valid(path: Path, keys: tuple[str, ...] = ()) -> bool:
    try:
        value = read_json(path)
        return all(key in value for key in keys)
    except Exception:
        return False


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_optional(value: str | None, *candidates: Path) -> Path | None:
    if value:
        path = Path(value).expanduser().resolve()
        return path if path.is_file() else None
    return next((path.resolve() for path in candidates if path.is_file()), None)


def stage_outputs(stage: str, config: dict[str, Any]) -> list[Path]:
    work = Path(config["work"])
    dynamic = {
        "preflight": [work / "environment_report.json"],
        "inspect": [work / "media_inventory.json"],
        "thumbnails": [work / "broll_index.json"],
        "analyze_broll": [work / "broll_analysis.json"],
        "transcribe": [work / "transcript.json"],
        "build_v1": [work / "script_v1.json"],
        "prepare_v1_correction": [work / "v1_correction_worksheet.json"],
        "correct_v1": [work / "script_v1_corrected.json"],
        "shorten_v2": [work / "script_v2.json", work / "script_v2_coherence_report.md"],
        "align": [work / "roughcut_plan.json"],
        "contact_sheets": [work / "broll_contact_sheets"],
        "apply_broll_labels": [work / "broll_index.json"],
        "match_broll": [work / "broll_matches.json"],
        "compose": [work / "edit_plan.json"],
        "editorial_audit": [work / "editorial_quality_report.json"],
        "chart_propose": [work / "chart_plan.json"],
        "chart_sync": [work / "chart_sync_report.json"],
        "chart_layout_prepare": [work / "chart_layout_review.json"],
        "chart_layout_apply": [work / "chart_plan.json"],
        "chart_render": [work / "chart_render_manifest.json"],
        "chart_attach": [work / "edit_plan_with_charts.json", work / "media_inventory_with_charts.json"],
        "branding": [work / "branding" / "branding_manifest.json"],
        "speaker_labels": [work / "edit_plan_with_charts.json"],
        "export_plain": [work / "jianying_plain" / "artifacts" / "jianying_export_report.json"],
        "export_encrypted": [work / "jianying_export_artifacts" / "jianying_export_report.json"],
    }
    return dynamic.get(stage, [])


def transcript_complete(work: Path) -> bool:
    inventory_path, transcript_path = work / "media_inventory.json", work / "transcript.json"
    if not json_valid(inventory_path, ("a_roll",)) or not json_valid(transcript_path, ("files",)):
        return False
    inventory = read_json(inventory_path)
    transcript = read_json(transcript_path)
    expected = {str(Path(item["path"]).resolve()).lower() for item in inventory.get("a_roll", [])}
    actual = {
        str(Path(item["source"]).resolve()).lower()
        for item in transcript.get("files", [])
        if item.get("segments")
    }
    return bool(expected) and expected == actual


def output_complete(stage: str, config: dict[str, Any]) -> bool:
    work = Path(config["work"])
    if stage == "preflight":
        path = work / "environment_report.json"
        return json_valid(path, ("ok",)) and bool(read_json(path).get("ok"))
    if stage == "transcribe":
        return transcript_complete(work)
    if stage == "apply_broll_labels":
        return (work / ".stages" / "apply_broll_labels.json").is_file() and json_valid(work / "broll_index.json", ("clips",))
    if stage == "editorial_audit":
        path = work / "editorial_quality_report.json"
        return json_valid(path, ("ready",)) and bool(read_json(path).get("ready"))
    if stage == "chart_sync":
        path = work / "chart_sync_report.json"
        if not json_valid(path):
            return False
        report = read_json(path)
        return not report.get("review_required") and not report.get("errors") and not report.get("failures")
    if stage == "compose":
        path = work / "edit_plan.json"
        if not json_valid(path, ("tracks", "b_roll_metrics")):
            return False
        metrics = read_json(path).get("b_roll_metrics", {})
        if not metrics.get("distribution_balanced") or not metrics.get("source_use_limit_respected"):
            return False
        if config.get("case_type") == "ai":
            used = int((metrics.get("common_ai_quota") or {}).get("used", 0))
            if not 3 <= used <= 5:
                return False
        return True
    if stage == "chart_layout_apply":
        path = work / "chart_plan.json"
        if not json_valid(path, ("charts",)):
            return False
        plan = read_json(path)
        return plan.get("layout_policy") == "subject_safe" and all(item.get("layout") for item in plan["charts"])
    if stage == "speaker_labels":
        path = work / "edit_plan_with_charts.json"
        if not json_valid(path, ("tracks",)):
            return False
        graphics = read_json(path).get("tracks", {}).get("graphics", [])
        return any(item.get("template") == "speaker_label" for item in graphics)
    outputs = stage_outputs(stage, config)
    return bool(outputs) and all(path.exists() and (path.is_dir() or path.stat().st_size > 0) for path in outputs)


def tail(path: Path, lines: int = 24) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except Exception:
        return "（无法读取日志）"


def command_for(stage: str, config: dict[str, Any]) -> list[str] | None:
    py = sys.executable
    work, project = config["work"], config["project"]
    pipeline = str(SCRIPTS / "pipeline.py")
    common = ["--project", project, "--work", work]
    if config.get("case_type"):
        common += ["--case-type", config["case_type"]]
    speaker_map = resolve_optional(config.get("speaker_map"), Path(work) / "speaker_map.json", Path(project) / "speaker_map.json")
    corrections = resolve_optional(config.get("corrections"), Path(work) / "transcript_corrections.json", Path(project) / "transcript_corrections.json")
    broll_labels = resolve_optional(config.get("broll_labels"), Path(work) / "broll_labels.json")
    commands: dict[str, list[str]] = {
        "preflight": [py, str(SCRIPTS / "doctor.py"), "--strict", "--project", project, "--work", work,
                      "--model", config["model"], "--model-cache", config["model_cache"],
                      "--json-out", str(Path(work) / "environment_report.json")],
        "inspect": [py, pipeline, "inspect", *common],
        "thumbnails": [py, pipeline, "thumbnails", *common],
        "analyze_broll": [py, pipeline, "analyze-broll", *common],
        "transcribe": [py, pipeline, "transcribe", *common, "--model", config["model"], "--language", "zh", "--device", "cpu", "--compute-type", "int8"],
        "build_v1": [py, pipeline, "build-v1", *common],
        "prepare_v1_correction": [py, pipeline, "prepare-v1-correction", *common],
        "correct_v1": [py, pipeline, "correct-v1", *common, "--review", str(Path(work) / "v1_correction_plan.json")],
        "shorten_v2": [py, pipeline, "shorten-script", *common, "--max-minutes", "6.5"],
        "align": [py, pipeline, "align", *common],
        "contact_sheets": [py, pipeline, "contact-sheets", *common],
        "apply_broll_labels": [py, pipeline, "apply-labels", *common, "--labels", str(broll_labels) if broll_labels else ""],
        "match_broll": [py, pipeline, "match-broll", *common, "--max-source-uses", "2"],
        "compose": [py, pipeline, "compose", *common, "--bridge-gap-seconds", "1.5", "--target-coverage", "0.5"],
        "editorial_audit": [py, str(SCRIPTS / "editorial_quality.py"), "audit", "--work", work],
        "chart_propose": [py, str(SCRIPTS / "chart_pipeline.py"), "propose", "--work", work, "--count", str(config["chart_count"]), "--motion"],
        "chart_sync": [py, str(SCRIPTS / "chart_pipeline.py"), "sync", "--work", work],
        "chart_layout_prepare": [py, str(SCRIPTS / "chart_layout.py"), "prepare", "--work", work],
        "chart_layout_apply": [py, str(SCRIPTS / "chart_layout.py"), "apply", "--work", work],
        "chart_render": [py, str(SCRIPTS / "chart_pipeline.py"), "render", "--work", work],
        "chart_attach": [py, str(SCRIPTS / "chart_pipeline.py"), "attach", "--work", work],
        "branding": [py, str(SCRIPTS / "branding.py"), "--project", project, "--work", work]
                    + (["--case-type", config["case_type"]] if config.get("case_type") else []),
        "speaker_labels": [py, str(SCRIPTS / "editorial_quality.py"), "signposts", "--work", work, "--replace-existing"],
        "export_plain": [py, str(SCRIPTS / "export_jianying.py"),
                         "--edit-plan", str(Path(work) / "edit_plan_with_charts.json"),
                         "--inventory", str(Path(work) / "media_inventory_with_charts.json"),
                         "--draft-root", str(Path(work) / "jianying_plain" / "Drafts"),
                         "--artifacts-dir", str(Path(work) / "jianying_plain" / "artifacts"),
                         "--draft-name", config["draft_name"] + "_结构测试", "--mode", "plain"],
        "export_encrypted": [py, str(SCRIPTS / "export_jianying.py"),
                             "--edit-plan", str(Path(work) / "edit_plan_with_charts.json"),
                             "--inventory", str(Path(work) / "media_inventory_with_charts.json"),
                             "--draft-root", config["draft_root"],
                             "--artifacts-dir", str(Path(work) / "jianying_export_artifacts"),
                             "--draft-name", config["draft_name"], "--mode", "encrypted",
                             "--jianying-install", config["jianying_install"]],
    }
    command = commands.get(stage)
    if stage == "build_v1" and command:
        if speaker_map:
            command += ["--speaker-map", str(speaker_map)]
        if corrections:
            command += ["--corrections", str(corrections)]
    if stage == "preflight" and command and config.get("jianying_install"):
        command += ["--jianying-install", config["jianying_install"], "--draft-root", config["draft_root"], "--require-jianying"]
    return command


def speaker_gate(config: dict[str, Any]) -> tuple[bool, str]:
    work, project = Path(config["work"]), Path(config["project"])
    path = resolve_optional(config.get("speaker_map"), work / "speaker_map.json", project / "speaker_map.json")
    if path and json_valid(path):
        config["speaker_map"] = str(path)
        return True, "speaker_map.json 已就绪"
    return False, (
        "请根据 transcript.json、V0身份信息和A-roll代表帧，在工作目录创建 speaker_map.json。\n"
        "必须逐文件映射人物，并写明职位优先级；不要安装人脸或声纹模型。"
    )


def file_gate(path: Path, instruction: str, validator: Callable[[Path], bool] = lambda value: json_valid(value)) -> tuple[bool, str]:
    return (True, f"已就绪：{path.name}") if validator(path) else (False, instruction)


def chart_approved(path: Path) -> bool:
    if not json_valid(path, ("charts",)):
        return False
    plan = read_json(path)
    charts = plan.get("charts", [])
    return bool(charts) and not plan.get("review_required") and all(item.get("status") == "approved" for item in charts)


def layout_approved(path: Path) -> bool:
    if not json_valid(path, ("charts",)):
        return False
    review = read_json(path)
    return bool(review["charts"]) and all(
        item.get("status") == "approved"
        and all(isinstance(sample.get("protected_boxes"), list) for sample in item.get("samples", []))
        for item in review["charts"]
    )


def presentation_approved(path: Path, edit_plan_path: Path | None = None) -> bool:
    if not json_valid(path, ("cards", "source_edit_plan_sha256")):
        return False
    value = read_json(path)
    if edit_plan_path is not None and (
        not edit_plan_path.is_file()
        or value.get("source_edit_plan_sha256") != file_sha256(edit_plan_path)
    ):
        return False
    cards = [item for item in value["cards"] if item.get("kind") == "speaker"]
    return bool(cards) and all(item.get("status") == "approved" and item.get("evidence") for item in cards)


def gate_for(stage: str, config: dict[str, Any]) -> tuple[bool, str] | None:
    work = Path(config["work"])
    if stage == "speaker_review":
        return speaker_gate(config)
    if stage == "v1_review":
        return file_gate(work / "v1_correction_plan.json", "请审核 v1_correction_worksheet.md，生成 v1_correction_plan.json；只修正ASR错误，不从V0补写口播。")
    if stage == "v2_review":
        return file_gate(work / "v2_editorial_plan.json", "请通读 script_v1_corrected.md，生成 v2_editorial_plan.json；第一个block必须是最高职位者的公司介绍并填写 purpose=company_intro，保持完整语义段、主讲人最长、其他人物时长比不超过1.35。")
    if stage == "broll_review":
        supplied = resolve_optional(config.get("broll_labels"), work / "broll_labels.json", Path(config["project"]) / "broll_labels.json")
        if supplied:
            config["broll_labels"] = str(supplied)
            return True, f"已就绪：{supplied}"
        inventory = read_json(work / "media_inventory.json") if json_valid(work / "media_inventory.json") else {}
        case_specific = [item for item in inventory.get("b_roll", []) if item.get("origin") != "skill_common_ai"]
        if not case_specific:
            empty = work / "broll_labels.json"
            write_json(empty, {"schema_version": 1, "groups": []})
            config["broll_labels"] = str(empty)
            return True, "没有案例自身B-roll，使用空标签表；通用AI录屏沿用内置标签。"
        return False, "请查看 broll_contact_sheets，为案例自身B-roll生成 broll_labels.json。通用AI录屏已有内置标签，不要重复标注。"
    if stage == "chart_review":
        return file_gate(work / "chart_plan.json", "请逐项核对 chart_plan.json 的事实、单位、前后关系和口播cues，并检查图表覆盖前中后三段；仅将3–5个短促、同构且连续表达的真实排比句设为 parallelism，为它补齐 title_en 和逐项对应的 items_en，普通并列信息仍用 list；将确认项设为 approved，并把顶层 review_required 设为 false。", chart_approved)
    if stage == "chart_layout_review":
        return file_gate(work / "chart_layout_review.json", "请查看 layout_review 下的全部图表联系图，为每个样本填写人物/重点对象 protected_boxes；无人物时填 []，并将图表 status 设为 approved。", layout_approved)
    if stage == "speaker_label_review":
        return file_gate(
            work / "presentation_notes.json",
            "请从V0身份标题和最终A-roll生成 presentation_notes.json，只保留人物姓名、职务、公司标签，不生成开头主题标签；如果剪辑计划已变化，重新核对并更新 source_edit_plan_sha256。",
            lambda path: presentation_approved(path, work / "edit_plan.json"),
        )
    return None


def should_skip(stage: str, config: dict[str, Any]) -> bool:
    if config.get("chart_count", 0) == 0 and stage.startswith("chart_"):
        return True
    if config.get("skip_branding") and stage == "branding":
        return True
    if config.get("skip_speaker_labels") and stage in {"speaker_label_review", "speaker_labels"}:
        return True
    if config.get("skip_export") and stage in {"export_plain", "export_encrypted"}:
        return True
    return False


def prepare_no_chart_base(config: dict[str, Any]) -> None:
    if config.get("chart_count", 0) != 0:
        return
    work = Path(config["work"])
    if (work / "edit_plan.json").exists() and not (work / "edit_plan_with_charts.json").exists():
        shutil.copy2(work / "edit_plan.json", work / "edit_plan_with_charts.json")
    if (work / "media_inventory.json").exists() and not (work / "media_inventory_with_charts.json").exists():
        shutil.copy2(work / "media_inventory.json", work / "media_inventory_with_charts.json")


def write_stage_marker(work: Path, stage: str) -> None:
    write_json(work / ".stages" / f"{stage}.json", {"stage": stage, "completed_at": now()})


def write_next_action(work: Path, stage: str, instruction: str) -> None:
    content = f"# 当前暂停点\n\n- 阶段：`{stage}`\n- 状态：等待语义审核或必需输入\n\n## 下一步\n\n{instruction}\n\n完成后重新运行：\n\n```powershell\n.\\scripts\\run_case.ps1 -Action resume -Work \"{work}\"\n```\n"
    (work / NEXT_ACTION_FILE).write_text(content, encoding="utf-8")


def run_command(stage: str, command: list[str], config: dict[str, Any]) -> tuple[bool, str]:
    work = Path(config["work"])
    log_path = work / "logs" / f"{stage}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "HF_HOME": config["model_cache"],
        "HUGGINGFACE_HUB_CACHE": str(Path(config["model_cache"]) / "hub"),
    })
    with log_path.open("w", encoding="utf-8") as log:
        log.write("COMMAND: " + subprocess.list2cmdline(command) + "\n\n")
        try:
            result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        except Exception:
            log.write(traceback.format_exc())
            return False, str(log_path)
    return result.returncode == 0, str(log_path)


def resume(state: dict[str, Any], retry_failed: bool = False, stop_after: str | None = None) -> int:
    config, work = state["config"], Path(state["config"]["work"])
    prepare_no_chart_base(config)
    for stage in STAGES:
        prepare_no_chart_base(config)
        record = state["stages"].setdefault(stage, {"status": "pending", "attempts": 0})
        if should_skip(stage, config):
            record.update({"status": "skipped", "updated_at": now()})
            save_state(state)
            continue
        if record["status"] in {"passed", "skipped"}:
            if stop_after == stage:
                break
            continue
        if record["status"] == "failed" and not retry_failed:
            print(f"阶段 {stage} 上次失败。查看 {record.get('log')}；确认修复后使用 -RetryFailed。")
            return 2
        if int(record.get("attempts", 0)) >= MAX_ATTEMPTS:
            print(f"阶段 {stage} 已失败 {MAX_ATTEMPTS} 次，产品保护已停止自动重试。请人工检查日志：{record.get('log')}")
            return 2

        gate = gate_for(stage, config)
        if gate is not None:
            ready, instruction = gate
            if not ready:
                record.update({"status": "waiting_review", "updated_at": now(), "instruction": instruction})
                state["current_stage"] = stage
                save_state(state)
                write_next_action(work, stage, instruction)
                print(f"已安全暂停在 {stage}。下一步说明：{work / NEXT_ACTION_FILE}")
                return 3
            record.update({"status": "passed", "updated_at": now(), "note": instruction})
            save_state(state)
            if stop_after == stage:
                break
            continue

        if output_complete(stage, config):
            record.update({"status": "passed", "updated_at": now(), "note": "采用已有且完整的阶段产物"})
            save_state(state)
            print(f"[采用已有产物] {stage}")
            if stop_after == stage:
                break
            continue

        command = command_for(stage, config)
        if command is None:
            raise RuntimeError(f"阶段没有命令：{stage}")
        if stage == "apply_broll_labels" and command[-1] == "":
            raise RuntimeError("broll_review 已通过但找不到 broll_labels.json")
        print(f"[运行] {stage}")
        record["attempts"] = int(record.get("attempts", 0)) + 1
        record["started_at"] = now()
        save_state(state)
        ok, log_path = run_command(stage, command, config)
        if ok and stage in {"apply_broll_labels"}:
            write_stage_marker(work, stage)
        if not ok or not output_complete(stage, config):
            record.update({"status": "failed", "updated_at": now(), "log": log_path})
            state["current_stage"] = stage
            save_state(state)
            print(f"阶段 {stage} 失败，已停止，不会切换模型或重装环境。日志：{log_path}")
            print(tail(Path(log_path)))
            return 2
        record.update({"status": "passed", "updated_at": now(), "log": log_path})
        state["current_stage"] = stage
        save_state(state)
        if stop_after == stage:
            break

    if all(item.get("status") in {"passed", "skipped"} for item in state["stages"].values()):
        state["status"] = "complete"
        state["current_stage"] = "complete"
        save_state(state)
        (work / NEXT_ACTION_FILE).unlink(missing_ok=True)
        print(f"全部阶段完成。运行状态：{work / STATE_FILE}")
    return 0


def create_state(args: argparse.Namespace) -> dict[str, Any]:
    project, work = args.project.resolve(), args.work.resolve()
    if project == work or project in work.parents:
        raise ValueError("工作目录不能等于素材目录，也不能位于素材目录内部。")
    work.mkdir(parents=True, exist_ok=True)
    path = state_path(work)
    if path.exists() and not args.replace_state:
        raise FileExistsError(f"运行状态已存在：{path}。请使用 resume；不要重新开始。")
    runtime_path = ROOT / ".runtime.json"
    runtime = read_json(runtime_path) if runtime_path.is_file() else {}
    cache_base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) / "BenchmarkCaseVideoEditor" / "models"
    config = {
        "project": str(project),
        "work": str(work),
        "draft_root": str(args.draft_root.resolve()) if args.draft_root else "",
        "jianying_install": str(args.jianying_install.resolve()) if args.jianying_install else "",
        "draft_name": args.draft_name or project.name,
        "case_type": getattr(args, "case_type", None),
        "model": args.model or runtime.get("model", "small"),
        "model_cache": str(Path(runtime.get("model_cache", cache_base)).resolve()),
        "speaker_map": str(args.speaker_map.resolve()) if args.speaker_map else None,
        "corrections": str(args.corrections.resolve()) if args.corrections else None,
        "broll_labels": str(args.broll_labels.resolve()) if args.broll_labels else None,
        "chart_count": args.chart_count,
        "skip_branding": args.skip_branding,
        "skip_speaker_labels": args.skip_speaker_labels,
        "skip_export": args.skip_export,
    }
    if not args.skip_export and (not config["draft_root"] or not config["jianying_install"]):
        raise ValueError("导出剪映草稿必须提供 --draft-root 和 --jianying-install；或显式使用 --skip-export。")
    state = {
        "schema_version": 1,
        "skill_version": VERSION,
        "created_at": now(),
        "updated_at": now(),
        "status": "running",
        "current_stage": "preflight",
        "config": config,
        "stages": {stage: {"status": "pending", "attempts": 0} for stage in STAGES},
    }
    save_state(state)
    return state


def print_status(state: dict[str, Any]) -> None:
    print(f"Skill版本：{state.get('skill_version')}  总状态：{state.get('status')}  当前：{state.get('current_stage')}")
    for stage in STAGES:
        record = state["stages"].get(stage, {})
        print(f"- {stage:24} {record.get('status', 'pending'):14} 尝试 {record.get('attempts', 0)}/{MAX_ATTEMPTS}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="标杆案例自动剪辑产品运行器")
    sub = parser.add_subparsers(dest="action", required=True)
    start = sub.add_parser("start", help="创建运行状态并执行到下一个审核关口")
    start.add_argument("--project", type=Path, required=True)
    start.add_argument("--work", type=Path, required=True)
    start.add_argument("--draft-root", type=Path)
    start.add_argument("--jianying-install", type=Path)
    start.add_argument("--draft-name")
    start.add_argument("--case-type", choices=("ai", "production"))
    start.add_argument("--model", choices=("tiny", "base", "small", "medium"))
    start.add_argument("--speaker-map", type=Path)
    start.add_argument("--corrections", type=Path)
    start.add_argument("--broll-labels", type=Path)
    start.add_argument("--chart-count", type=int, default=8, choices=range(0, 11))
    start.add_argument("--skip-branding", action="store_true")
    start.add_argument("--skip-speaker-labels", action="store_true")
    start.add_argument("--skip-export", action="store_true")
    start.add_argument("--replace-state", action="store_true")
    start.add_argument("--stop-after", choices=STAGES)

    resume_parser = sub.add_parser("resume", help="从状态文件继续，不重复已完成阶段")
    resume_parser.add_argument("--work", type=Path, required=True)
    resume_parser.add_argument("--retry-failed", action="store_true")
    resume_parser.add_argument("--stop-after", choices=STAGES)

    status = sub.add_parser("status", help="显示阶段状态")
    status.add_argument("--work", type=Path, required=True)

    reset = sub.add_parser("reset", help="仅重置指定阶段及其后续状态，不删除产物")
    reset.add_argument("--work", type=Path, required=True)
    reset.add_argument("--from-stage", required=True, choices=STAGES)
    return parser


def assert_product_runtime() -> None:
    actual = Path(sys.executable).resolve()
    expected = (ROOT / ".venv").resolve()
    if len(actual.parents) < 2 or actual.parents[1] != expected:
        raise RuntimeError(
            f"必须使用 Skill 独立环境运行。当前：{actual}；期望：{expected}. "
            "请使用 scripts/run_case.ps1。"
        )
    if not (ROOT / ".runtime.json").is_file():
        raise RuntimeError("缺少 .runtime.json。请先运行 scripts/setup.ps1 完成一次性安装。")


def main() -> int:
    args = build_parser().parse_args()
    assert_product_runtime()
    if args.action == "start":
        return resume(create_state(args), stop_after=args.stop_after)
    work = args.work.resolve()
    state = load_state(work)
    if args.action == "resume":
        return resume(state, retry_failed=args.retry_failed, stop_after=args.stop_after)
    if args.action == "status":
        print_status(state)
        return 0
    start_index = STAGES.index(args.from_stage)
    for stage in STAGES[start_index:]:
        state["stages"][stage] = {"status": "pending", "attempts": 0}
    state["status"] = "running"
    state["current_stage"] = args.from_stage
    save_state(state)
    print(f"已从 {args.from_stage} 重置状态；已有文件没有删除。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
