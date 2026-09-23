#!/usr/bin/env bash
set -euo pipefail

skill_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON_BIN:-python3}"

if ! command -v "$python_bin" >/dev/null 2>&1; then
  echo "未找到 Python 3。请先安装 Python 3.10 或更高版本。" >&2
  exit 1
fi

"$python_bin" -m venv "$skill_root/.venv"
venv_python="$skill_root/.venv/bin/python"
"$venv_python" -m pip install --upgrade pip
"$venv_python" -m pip install -r "$skill_root/requirements.txt"

echo "环境已准备完成：$venv_python"
echo "macOS/Linux 可运行转写、剪辑决策、B-roll、字幕和图表流程。"
echo "剪映可编辑草稿导出目前仅在 Windows 剪映专业版 11.4.2/11.5.0 验证。"
