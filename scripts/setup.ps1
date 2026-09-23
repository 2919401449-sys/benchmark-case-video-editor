param(
    [string]$SkillRoot = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
)

$ErrorActionPreference = "Stop"

function Find-Python {
    foreach ($name in @("py", "python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }

    $runtime = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (Test-Path -LiteralPath $runtime) { return $runtime }

    throw "未找到 Python 3.10 或更高版本。请先安装 Python，然后重新运行本脚本。"
}

$python = Find-Python
$venv = Join-Path $SkillRoot ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    & $python -m venv $venv
}

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip 升级失败，请检查网络连接。" }
& $venvPython -m pip install -r (Join-Path $SkillRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败，请检查网络连接。" }

Write-Output "环境已准备完成：$venvPython"
Write-Output "第一次语音识别时会自动下载 Whisper 模型。"
