param(
    [string]$Tester = "",
    [string]$RuntimeRoot = "F:\CodexProjects\yunpai-ecommerce-agent-m8r-runtime",
    [switch]$AutoConfirm
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$scenario = Get-ChildItem -LiteralPath $PSScriptRoot -Filter "WP2_*.py" |
    Select-Object -First 1 -ExpandProperty FullName

if (-not (Test-Path -LiteralPath $python)) {
    throw "M8-R Python environment was not found: $python"
}
if (-not $scenario -or -not (Test-Path -LiteralPath $scenario)) {
    throw "WP2 manual acceptance scenario was not found."
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dataDir = Join-Path $RuntimeRoot "wp2-manual-data\$stamp"
$evidenceDir = Join-Path $RuntimeRoot "wp2-manual-evidence"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
New-Item -ItemType Directory -Force -Path $evidenceDir | Out-Null

$arguments = @(
    $scenario,
    "--data-dir", $dataDir,
    "--evidence-dir", $evidenceDir
)
if (-not [string]::IsNullOrWhiteSpace($Tester)) {
    $arguments += @("--tester", $Tester)
}
if ($AutoConfirm) {
    $arguments += "--auto-confirm"
}

Write-Host "M8-R WP2 manual acceptance is starting." -ForegroundColor Cyan
Write-Host "Isolated data directory: $dataDir"
Write-Host "Evidence directory: $evidenceDir"
Write-Host "No model, real store, or platform write action is used."

& $python @arguments
exit $LASTEXITCODE
