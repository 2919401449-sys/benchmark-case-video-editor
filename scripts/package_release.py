#!/usr/bin/env python3
"""Create a clean GitHub-ready folder and ZIP without local environments."""
from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INCLUDE_DIRS = ("agents", "assets", "references", "scripts", "vendor")
INCLUDE_FILES = (
    ".gitattributes",
    ".gitignore",
    "README.md",
    "SKILL.md",
    "THIRD_PARTY_NOTICES.md",
    "VERSION",
    "requirements.txt",
)


def ignored(_directory: str, names: list[str]) -> set[str]:
    result = set()
    for name in names:
        if name in {"__pycache__", ".pytest_cache", ".venv", "dist", "release", "work"}:
            result.add(name)
        elif name.endswith((".pyc", ".pyo", ".log", ".zip")):
            result.add(name)
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 GitHub 发布目录和 ZIP")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", default=(ROOT / "VERSION").read_text(encoding="utf-8").strip())
    args = parser.parse_args()
    package_name = f"benchmark-case-video-editor-v{args.version}"
    target = args.output.resolve() / package_name
    if target.exists() or target.with_suffix(".zip").exists():
        raise FileExistsError(f"为避免覆盖，发布目标已存在：{target}")
    target.mkdir(parents=True)
    for name in INCLUDE_FILES:
        shutil.copy2(ROOT / name, target / name)
    for name in INCLUDE_DIRS:
        shutil.copytree(ROOT / name, target / name, ignore=ignored)

    files = sorted(path for path in target.rglob("*") if path.is_file())
    manifest = target / "RELEASE_MANIFEST.sha256"
    manifest.write_text(
        "\n".join(f"{sha256(path)}  {path.relative_to(target).as_posix()}" for path in files) + "\n",
        encoding="utf-8",
    )
    archive = shutil.make_archive(str(target), "zip", root_dir=target.parent, base_dir=target.name)
    print(f"发布目录：{target}")
    print(f"ZIP：{archive}")
    print(f"文件数：{len(files) + 1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
