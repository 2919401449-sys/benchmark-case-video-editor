#!/usr/bin/env python3
"""Check whether the cloned skill has the files needed for local execution."""
from __future__ import annotations

import argparse
import importlib
import platform
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def mark(ok: bool) -> str:
    return "通过" if ok else "缺失"


def main() -> int:
    parser = argparse.ArgumentParser(description="检查标杆案例自动剪辑 Skill 环境")
    parser.add_argument("--jianying-install", type=Path, help="Windows 剪映版本目录")
    args = parser.parse_args()
    checks: list[tuple[str, bool, str]] = []

    checks.append(("Python 3.10+", sys.version_info >= (3, 10), platform.python_version()))
    for module in ("PIL", "numpy", "faster_whisper", "imageio_ffmpeg", "opencc"):
        try:
            importlib.import_module(module)
            checks.append((f"Python 依赖 {module}", True, "可导入"))
        except Exception as exc:  # environment diagnostics should continue
            checks.append((f"Python 依赖 {module}", False, str(exc)))

    vendor = ROOT / "vendor" / "pyJianYingDraft-aoguai" / "pyJianYingDraft" / "__init__.py"
    checks.append(("剪映草稿第三方库", vendor.is_file(), str(vendor)))
    manifest = ROOT / "assets" / "common-broll" / "ai-design-platform" / "manifest.json"
    checks.append(("通用 AI 录屏清单", manifest.is_file(), str(manifest)))

    try:
        from pipeline import find_ffmpeg

        ffmpeg = Path(find_ffmpeg())
        checks.append(("FFmpeg", ffmpeg.is_file(), str(ffmpeg)))
    except Exception as exc:
        checks.append(("FFmpeg", False, str(exc)))

    is_windows = platform.system() == "Windows"
    if args.jianying_install:
        dll = args.jianying_install / "videoeditor.dll"
        checks.append(("剪映加密 DLL", is_windows and dll.is_file(), str(dll)))
    elif is_windows:
        checks.append(("剪映加密 DLL", False, "未传 --jianying-install；核心流程仍可运行"))
    else:
        checks.append(("剪映草稿导出", False, "当前只支持 Windows；核心流程可运行"))

    for name, ok, detail in checks:
        print(f"[{mark(ok)}] {name}: {detail}")
    required = [ok for name, ok, _ in checks if name != "剪映加密 DLL" and name != "剪映草稿导出"]
    return 0 if all(required) else 1


if __name__ == "__main__":
    raise SystemExit(main())
