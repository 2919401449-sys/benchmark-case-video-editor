param(
    [string]$SkillRoot = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)),
    [string]$JianyingInstall,
    [ValidateSet("tiny", "base", "small", "medium")]
    [string]$Model = "small",
    [string]$CacheRoot,
    [switch]$SkipModelDownload,
    [switch]$Recreate
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$SkillRoot = (Resolve-Path -LiteralPath $SkillRoot).Path
$venv = Join-Path $SkillRoot ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$lockFile = Join-Path $SkillRoot "requirements-lock.txt"

if ($JianyingInstall) {
    if (-not (Test-Path -LiteralPath $JianyingInstall -PathType Container)) {
        throw "剪映安装目录不存在或不是文件夹：$JianyingInstall。请填写包含 videoeditor.dll 的版本目录，不要填写开始菜单或桌面快捷方式。"
    }
    $JianyingInstall = (Resolve-Path -LiteralPath $JianyingInstall).Path
    if (-not (Test-Path -LiteralPath (Join-Path $JianyingInstall "videoeditor.dll") -PathType Leaf)) {
        throw "剪映目录内没有 videoeditor.dll：$JianyingInstall。请在剪映实际安装目录中找到包含该 DLL 的版本文件夹；不要使用开始菜单 Programs 目录。"
    }
}

function Test-PythonCandidate {
    param([string]$Exe, [string[]]$Prefix)
    try {
        $probe = & $Exe @Prefix -c "import json,platform,sys; print(json.dumps({'version':list(sys.version_info[:3]),'bits':platform.architecture()[0],'exe':sys.executable}))" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $probe) { return $null }
        $data = $probe | ConvertFrom-Json
        $major = [int]$data.version[0]
        $minor = [int]$data.version[1]
        if ($major -ne 3 -or $minor -lt 11 -or $minor -gt 12) { return $null }
        if ($data.bits -ne "64bit") { return $null }
        $normalized = [string]$data.exe
        if ($normalized -match "\\WindowsApps\\" -or $normalized -match "\\.cache\\codex-runtimes\\") { return $null }
        return [pscustomobject]@{ Exe=$Exe; Prefix=$Prefix; Path=$normalized; Version="$major.$minor.$($data.version[2])" }
    } catch {
        return $null
    }
}

function Find-ProductPython {
    $candidates = @(
        @{Exe="py"; Prefix=@("-3.12")},
        @{Exe="py"; Prefix=@("-3.11")},
        @{Exe="python"; Prefix=@()},
        @{Exe="python3"; Prefix=@()}
    )
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
        $result = Test-PythonCandidate -Exe $candidate.Exe -Prefix $candidate.Prefix
        if ($result) { return $result }
    }
    throw @"
没有找到可用于产品环境的 64 位 Python 3.11 或 3.12。
请从 https://www.python.org/downloads/windows/ 安装 64 位 Python 3.12，安装时勾选 Add python.exe to PATH，然后重新运行 setup.ps1。
为避免 DLL 和 CUDA 冲突，本脚本不会使用 Windows Store 占位程序或 Codex 自带的缓存 Python。
"@
}

if (-not (Test-Path -LiteralPath $lockFile)) {
    throw "缺少依赖锁定文件：$lockFile"
}

if ($Recreate -and (Test-Path -LiteralPath $venv)) {
    $resolvedVenv = [System.IO.Path]::GetFullPath($venv)
    $resolvedRoot = [System.IO.Path]::GetFullPath($SkillRoot)
    if (-not $resolvedVenv.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase) -or (Split-Path -Leaf $resolvedVenv) -ne ".venv") {
        throw "拒绝删除非 Skill 内部的虚拟环境：$resolvedVenv"
    }
    Remove-Item -LiteralPath $resolvedVenv -Recurse -Force
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    $python = Find-ProductPython
    Write-Output "使用独立 Python $($python.Version)：$($python.Path)"
    & $python.Exe @($python.Prefix) -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "创建虚拟环境失败。请确认 Python 安装包含 venv。" }
}

$venvInfo = & $venvPython -c "import json,platform,sys; print(json.dumps({'version':platform.python_version(),'bits':platform.architecture()[0],'exe':sys.executable}))" | ConvertFrom-Json
if ($venvInfo.bits -ne "64bit") { throw "虚拟环境不是 64 位：$($venvInfo.exe)" }
if ([version]$venvInfo.version -lt [version]"3.11" -or [version]$venvInfo.version -ge [version]"3.13") {
    throw "现有 .venv 使用 Python $($venvInfo.version)，产品版只支持 3.11/3.12。请使用 -Recreate 重建。"
}

$cacheBase = if ($CacheRoot) { $CacheRoot } elseif ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $env:USERPROFILE ".cache" }
$runtimeRoot = if ($CacheRoot) { $cacheBase } else { Join-Path $cacheBase "BenchmarkCaseVideoEditor" }
$runtimeRoot = [System.IO.Path]::GetFullPath($runtimeRoot)
$modelCache = Join-Path $runtimeRoot "models"
New-Item -ItemType Directory -Path $modelCache -Force | Out-Null
$env:HF_HOME = $modelCache
$env:HUGGINGFACE_HUB_CACHE = Join-Path $modelCache "hub"

Write-Output "安装经过锁定的依赖版本……"
$lockCheck = @'
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys
missing=[]
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line=raw.strip()
    if not line or line.startswith("#") or "==" not in line:
        continue
    name, expected=(part.strip() for part in line.split("==",1))
    try:
        actual=version(name)
    except PackageNotFoundError:
        actual="missing"
    if actual != expected:
        missing.append(f"{name}: {actual} -> {expected}")
if missing:
    print("\n".join(missing))
    raise SystemExit(1)
'@
& $venvPython -c $lockCheck $lockFile
$dependenciesReady = $LASTEXITCODE -eq 0
if (-not $dependenciesReady) {
    & $venvPython -m pip --version
    if ($LASTEXITCODE -ne 0) { throw "虚拟环境缺少 pip。请使用 -Recreate 重建。" }
    & $venvPython -m pip install --disable-pip-version-check -r $lockFile
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败。不要在正式案例任务中继续重试，请检查网络或Python版本。" }
} else {
    Write-Output "锁定依赖已经完整，跳过联网安装。"
}

$doctorArgs = @(
    (Join-Path $SkillRoot "scripts\doctor.py"),
    "--strict",
    "--model", $Model,
    "--model-cache", $modelCache,
    "--json-out", (Join-Path $runtimeRoot "environment_report.json")
)
if (-not $SkipModelDownload) { $doctorArgs += "--load-model" }
if ($JianyingInstall) { $doctorArgs += @("--jianying-install", $JianyingInstall, "--require-jianying") }

Write-Output "运行产品环境自检……"
& $venvPython @doctorArgs
if ($LASTEXITCODE -ne 0) {
    throw "环境自检未通过。请查看上面的缺失项；不要直接开始正式案例。"
}

# Only mark the product runtime ready after every required check succeeds.
# A failed first install must not leave a stale marker that lets a case start.
$runtime = [ordered]@{
    schema_version = 2
    created_at = (Get-Date).ToString("o")
    skill_root = $SkillRoot
    python = $venvInfo.exe
    python_version = $venvInfo.version
    architecture = $venvInfo.bits
    model = $Model
    model_cache = $modelCache
    requirements_lock = $lockFile
    setup_complete = $true
    jianying_install = if ($JianyingInstall) { $JianyingInstall } else { "" }
}
$runtime | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $SkillRoot ".runtime.json") -Encoding UTF8

Write-Output ""
Write-Output "环境准备完成：$venvPython"
Write-Output "模型缓存目录：$modelCache"
Write-Output "以后处理案例时只使用 scripts\run_case.ps1，不要让 Codex 临时安装或切换 Python。"
