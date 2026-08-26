param(
    [string]$Tester = "谢良璇",
    [string]$RuntimeRoot = "F:\CodexProjects\yunpai-ecommerce-agent-m8r-runtime",
    [switch]$AutoConfirm
)

$ErrorActionPreference = "Stop"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$scenario = Join-Path $PSScriptRoot "PR25_P1_承诺边界人工复验.py"

if (-not (Test-Path -LiteralPath $python)) {
    throw "M8-R Python environment was not found: $python"
}
if (-not (Test-Path -LiteralPath $scenario)) {
    throw "PR #25 P1 manual recheck scenario was not found: $scenario"
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dataDir = Join-Path $RuntimeRoot "pr25-p1-manual-data\$stamp"
$evidenceDir = Join-Path $RuntimeRoot "pr25-p1-manual-evidence"
$transcriptPath = Join-Path $evidenceDir ($Tester + "_PR25_P1承诺边界人工复验过程_" + $stamp + ".txt")
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
New-Item -ItemType Directory -Force -Path $evidenceDir | Out-Null

$arguments = @(
    $scenario,
    "--data-dir", $dataDir,
    "--evidence-dir", $evidenceDir,
    "--transcript", $transcriptPath,
    "--tester", $Tester
)
if ($AutoConfirm) {
    $arguments += "--auto-confirm"
}

Write-Host "PR #25 P1 commitment boundary manual recheck is starting." -ForegroundColor Cyan
Write-Host "Tester: $Tester"
Write-Host "Isolated data directory: $dataDir"
Write-Host "Evidence directory: $evidenceDir"
Write-Host "Controlled local model only; no external model or platform write is used."

Start-Transcript -Path $transcriptPath -Force | Out-Null
try {
    & $python @arguments
    $exitCode = $LASTEXITCODE
}
finally {
    Stop-Transcript | Out-Null
}
exit $exitCode
