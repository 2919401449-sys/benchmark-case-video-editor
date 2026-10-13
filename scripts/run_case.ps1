param(
    [ValidateSet("start", "resume", "status", "reset")]
    [string]$Action = "resume",
    [string]$Project,
    [Parameter(Mandatory=$true)]
    [string]$Work,
    [string]$DraftRoot,
    [string]$JianyingInstall,
    [string]$DraftName,
    [ValidateSet("ai", "production")]
    [string]$CaseType,
    [ValidateSet("tiny", "base", "small", "medium")]
    [string]$Model,
    [string]$SpeakerMap,
    [string]$Corrections,
    [string]$BrollLabels,
    [ValidateRange(0, 10)]
    [int]$ChartCount = 8,
    [string]$StopAfter,
    [string]$FromStage,
    [switch]$RetryFailed,
    [switch]$SkipBranding,
    [switch]$SkipSpeakerLabels,
    [switch]$SkipExport,
    [switch]$ReplaceState
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $root ".venv\Scripts\python.exe"
$runtimePath = Join-Path $root ".runtime.json"
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $runtimePath)) {
    throw "Skill 尚未安装运行环境。请先在 Skill 目录运行 .\scripts\setup.ps1。"
}
$runtime = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
if (-not $runtime.setup_complete) {
    throw "Skill 环境没有通过完整自检。请重新运行 .\scripts\setup.ps1，不要在案例处理中临时安装依赖。"
}
if (-not $JianyingInstall -and $runtime.jianying_install) {
    $JianyingInstall = [string]$runtime.jianying_install
}
if ($Action -eq "start" -and -not $SkipExport) {
    if (-not $JianyingInstall) {
        throw "未提供剪映安装目录，且一次性安装记录中没有可用路径。请传入包含 videoeditor.dll 的版本目录。"
    }
    if (-not (Test-Path -LiteralPath $JianyingInstall -PathType Container) -or
        -not (Test-Path -LiteralPath (Join-Path $JianyingInstall "videoeditor.dll") -PathType Leaf)) {
        throw "剪映安装目录无效：$JianyingInstall。必须是包含 videoeditor.dll 的实际版本目录，不能是开始菜单或桌面快捷方式目录。"
    }
    $JianyingInstall = (Resolve-Path -LiteralPath $JianyingInstall).Path
}
$runner = Join-Path $root "scripts\case_runner.py"
$arguments = @($runner, $Action, "--work", $Work)
if ($Action -eq "start") {
    if (-not $Project) { throw "start 必须提供 -Project。" }
    $arguments += @("--project", $Project, "--chart-count", "$ChartCount")
    if ($DraftRoot) { $arguments += @("--draft-root", $DraftRoot) }
    if ($JianyingInstall) { $arguments += @("--jianying-install", $JianyingInstall) }
    if ($DraftName) { $arguments += @("--draft-name", $DraftName) }
    if ($CaseType) { $arguments += @("--case-type", $CaseType) }
    if ($Model) { $arguments += @("--model", $Model) }
    if ($SpeakerMap) { $arguments += @("--speaker-map", $SpeakerMap) }
    if ($Corrections) { $arguments += @("--corrections", $Corrections) }
    if ($BrollLabels) { $arguments += @("--broll-labels", $BrollLabels) }
    if ($SkipBranding) { $arguments += "--skip-branding" }
    if ($SkipSpeakerLabels) { $arguments += "--skip-speaker-labels" }
    if ($SkipExport) { $arguments += "--skip-export" }
    if ($ReplaceState) { $arguments += "--replace-state" }
}
if ($Action -eq "resume" -and $RetryFailed) { $arguments += "--retry-failed" }
if ($Action -eq "reset") {
    if (-not $FromStage) { throw "reset 必须提供 -FromStage。" }
    $arguments += @("--from-stage", $FromStage)
}
if ($StopAfter -and $Action -in @("start", "resume")) { $arguments += @("--stop-after", $StopAfter) }

& $python @arguments
exit $LASTEXITCODE
