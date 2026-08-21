param(
    [string]$Tester = "谢良璇",
    [string]$RuntimeRoot = "F:\CodexProjects\yunpai-ecommerce-agent-m8r-runtime",
    [switch]$AutoConfirm
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$scenario = Get-ChildItem -LiteralPath $PSScriptRoot -Filter "WP3_*.py" |
    Select-Object -First 1 -ExpandProperty FullName

if (-not (Test-Path -LiteralPath $python)) {
    throw "M8-R Python environment was not found: $python"
}
if (-not $scenario -or -not (Test-Path -LiteralPath $scenario)) {
    throw "WP3 manual acceptance scenario was not found."
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dataDir = Join-Path $RuntimeRoot "wp3-manual-data\$stamp"
$evidenceDir = Join-Path $RuntimeRoot "wp3-manual-evidence"
$transcriptPath = Join-Path $evidenceDir ($Tester + "_WP3人工验收过程_" + $stamp + ".txt")
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
New-Item -ItemType Directory -Force -Path $evidenceDir | Out-Null

$arguments = @(
    $scenario,
    "--data-dir", $dataDir,
    "--evidence-dir", $evidenceDir,
    "--transcript", $transcriptPath
)
if (-not [string]::IsNullOrWhiteSpace($Tester)) {
    $arguments += @("--tester", $Tester)
}
if ($AutoConfirm) {
    $arguments += "--auto-confirm"
}

Write-Host "M8-R WP3 manual acceptance is starting." -ForegroundColor Cyan
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
