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
    "requirements-lock.txt",
)


def ignored(_directory: str, names: list[str]) -> set[str]:
    result = set()
    for name in names:
        if name in {"__pycache__", ".pytest_cache", ".venv", ".git", ".product-cache", "dist", "release", "work"}:
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


def verify_release(root: Path) -> tuple[int, int]:
    root = root.resolve()
    manifest = root / "RELEASE_MANIFEST.sha256"
    if not manifest.is_file():
        raise FileNotFoundError(f"发布包缺少哈希清单：{manifest}")
    expected: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        expected[relative] = digest
    actual_files = sorted(path for path in root.rglob("*") if path.is_file() and path != manifest)
    actual_names = {path.relative_to(root).as_posix() for path in actual_files}
    if actual_names != set(expected):
        missing = sorted(set(expected) - actual_names)
        extra = sorted(actual_names - set(expected))
        raise ValueError(f"发布包文件集合不一致；缺失={missing}；多余={extra}")
    mismatched = [name for name, digest in expected.items() if sha256(root / name) != digest]
    if mismatched:
        raise ValueError(f"发布包哈希不一致：{mismatched}")
    forbidden = [path for path in root.rglob("*") if path.name in {".venv", ".runtime.json", ".product-cache", "__pycache__"}]
    if forbidden:
        raise ValueError(f"发布包混入本机运行文件：{forbidden}")
    largest = max((path.stat().st_size for path in actual_files), default=0)
    if largest > 25 * 1024 * 1024:
        raise ValueError(f"最大文件 {largest / 1024 / 1024:.2f} MiB，超过本项目约定的 GitHub 网页上传安全线 25 MiB。")
    file_count = len(actual_files) + 1
    if file_count > 100:
        raise ValueError(f"发布包共有 {file_count} 个文件，超过 GitHub 网页单次拖放的100文件安全线。")
    return file_count, largest


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 GitHub 发布目录和 ZIP")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path, help="只验证一个已经解压的发布目录")
    parser.add_argument("--version", default=(ROOT / "VERSION").read_text(encoding="utf-8").strip())
    args = parser.parse_args()
    if args.verify:
        count, largest = verify_release(args.verify)
        print(f"发布包验证通过：{args.verify.resolve()}")
        print(f"文件数：{count}；最大文件：{largest / 1024 / 1024:.2f} MiB")
        return 0
    if not args.output:
        parser.error("生成发布包时必须提供 --output")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if f"当前产品化版本：`{args.version}`" not in readme:
        raise ValueError("VERSION 与 README 当前产品化版本不一致。")
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
    file_count, largest = verify_release(target)
    archive = shutil.make_archive(str(target), "zip", root_dir=target.parent, base_dir=target.name)
    print(f"发布目录：{target}")
    print(f"ZIP：{archive}")
    print(f"文件数：{file_count}")
    print(f"最大文件：{largest / 1024 / 1024:.2f} MiB；可使用 GitHub 网页单次拖放上传")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
