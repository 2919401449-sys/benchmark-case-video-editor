#!/usr/bin/env python3
"""Strict, machine-readable preflight for the productized editing skill."""
from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import struct
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def writable_check(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="bce-doctor-", dir=path, delete=True) as handle:
            handle.write(b"ok")
            handle.flush()
        return True, str(path.resolve())
    except Exception as exc:
        return False, str(exc)


def add(checks: list[dict[str, Any]], name: str, ok: bool, detail: str, required: bool = True) -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail, "required": required})


def main() -> int:
    parser = argparse.ArgumentParser(description="检查标杆案例自动剪辑 Skill 产品环境")
    parser.add_argument("--strict", action="store_true", help="要求使用本 Skill 的 .venv，并严格检查产品环境")
    parser.add_argument("--project", type=Path, help="可选：只读素材目录")
    parser.add_argument("--work", type=Path, help="可选：工作目录")
    parser.add_argument("--draft-root", type=Path, help="可选：剪映草稿箱目录")
    parser.add_argument("--jianying-install", type=Path, help="Windows 剪映版本目录，目录内需有 videoeditor.dll")
    parser.add_argument("--require-jianying", action="store_true", help="把剪映 DLL 和草稿箱检查作为必需项")
    parser.add_argument("--model", default="small", choices=("tiny", "base", "small", "medium"))
    parser.add_argument("--model-cache", type=Path)
    parser.add_argument("--load-model", action="store_true", help="实际加载/下载模型，首次安装时建议启用")
    parser.add_argument("--json-out", type=Path, help="环境报告输出位置")
    args = parser.parse_args()

    checks: list[dict[str, Any]] = []
    python_ok = (3, 11) <= sys.version_info[:2] < (3, 13)
    add(checks, "Python 3.11/3.12", python_ok, platform.python_version())
    is_64 = struct.calcsize("P") * 8 == 64
    add(checks, "64位 Python", is_64, platform.architecture()[0])
    expected_venv = (ROOT / ".venv").resolve()
    actual_python = Path(sys.executable).resolve()
    in_skill_venv = expected_venv == actual_python.parents[1] if len(actual_python.parents) > 1 else False
    add(checks, "Skill 独立虚拟环境", in_skill_venv or not args.strict, f"当前：{actual_python}；期望：{expected_venv}")
    actual_text = str(actual_python).lower()
    forbidden = "\\.cache\\codex-runtimes\\" in actual_text or "\\windowsapps\\" in actual_text
    add(checks, "非临时/占位 Python", not forbidden, str(actual_python))

    versions: dict[str, str] = {}
    for module, distribution in (
        ("PIL", "Pillow"),
        ("numpy", "numpy"),
        ("faster_whisper", "faster-whisper"),
        ("ctranslate2", "ctranslate2"),
        ("imageio_ffmpeg", "imageio-ffmpeg"),
        ("opencc", "opencc-python-reimplemented"),
    ):
        try:
            importlib.import_module(module)
            try:
                from importlib.metadata import version
                versions[distribution] = version(distribution)
            except Exception:
                versions[distribution] = "unknown"
            add(checks, f"依赖 {distribution}", True, versions[distribution])
        except Exception as exc:
            add(checks, f"依赖 {distribution}", False, repr(exc))

    try:
        import ctranslate2
        compute_types = sorted(ctranslate2.get_supported_compute_types("cpu"))
        add(checks, "CPU int8 转写", "int8" in compute_types, ", ".join(compute_types))
    except Exception as exc:
        add(checks, "CPU int8 转写", False, repr(exc))

    vendor = ROOT / "vendor" / "pyJianYingDraft-aoguai" / "pyJianYingDraft" / "__init__.py"
    add(checks, "剪映草稿第三方库", vendor.is_file(), str(vendor))
    manifest = ROOT / "assets" / "common-broll" / "ai-design-platform" / "manifest.json"
    add(checks, "通用 AI 录屏清单", manifest.is_file(), str(manifest))

    try:
        from pipeline import find_ffmpeg
        ffmpeg = Path(find_ffmpeg())
        probe = subprocess.run([str(ffmpeg), "-version"], capture_output=True, text=True, timeout=15)
        detail = (probe.stdout or probe.stderr).splitlines()[0] if (probe.stdout or probe.stderr) else str(ffmpeg)
        add(checks, "FFmpeg 可执行", ffmpeg.is_file() and probe.returncode == 0, detail)
    except Exception as exc:
        add(checks, "FFmpeg 可执行", False, repr(exc))

    model_cache = (args.model_cache or Path(os.environ.get("HF_HOME", ROOT / ".model-cache"))).resolve()
    if args.load_model:
        try:
            os.environ["HF_HOME"] = str(model_cache)
            os.environ["HUGGINGFACE_HUB_CACHE"] = str(model_cache / "hub")
            from faster_whisper import WhisperModel
            WhisperModel(args.model, device="cpu", compute_type="int8", download_root=str(model_cache))
            add(checks, f"Whisper模型 {args.model}", True, str(model_cache))
        except Exception as exc:
            add(checks, f"Whisper模型 {args.model}", False, repr(exc))
    else:
        add(checks, f"Whisper模型 {args.model}", True, "未加载；正式运行前建议 setup.ps1 不使用 -SkipModelDownload", required=False)

    if args.project:
        project = args.project.resolve()
        aroll = project / "A-roll"
        media = [path for path in aroll.glob("*") if path.is_file()] if aroll.is_dir() else []
        add(checks, "素材目录 A-roll", aroll.is_dir() and bool(media), f"{aroll}；文件数 {len(media)}")
    if args.work:
        work = args.work.resolve()
        project = args.project.resolve() if args.project else None
        safe = project is None or (work != project and project not in work.parents)
        add(checks, "工作目录与素材分离", safe, str(work))
        ok, detail = writable_check(work)
        add(checks, "工作目录可写", ok, detail)
    if args.draft_root:
        ok, detail = writable_check(args.draft_root.resolve())
        add(checks, "剪映草稿箱可写", ok, detail)

    if platform.system() == "Windows":
        if args.jianying_install:
            dll = args.jianying_install.resolve() / "videoeditor.dll"
            add(checks, "剪映加密 DLL", dll.is_file(), str(dll))
        else:
            add(checks, "剪映加密 DLL", not args.require_jianying, "未传 --jianying-install；仅能运行核心流程", required=args.require_jianying)
    else:
        add(checks, "剪映草稿导出", True, "当前平台仅运行跨平台计划，不导出剪映草稿", required=False)

    required_failures = [item for item in checks if item["required"] and not item["ok"]]
    report = {
        "schema_version": 2,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "skill_root": str(ROOT),
        "python": str(actual_python),
        "versions": versions,
        "model": args.model,
        "model_cache": str(model_cache),
        "checks": checks,
        "ok": not required_failures,
        "required_failures": len(required_failures),
    }
    for item in checks:
        marker = "通过" if item["ok"] else ("警告" if not item["required"] else "失败")
        print(f"[{marker}] {item['name']}: {item['detail']}")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"环境报告：{args.json_out.resolve()}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
