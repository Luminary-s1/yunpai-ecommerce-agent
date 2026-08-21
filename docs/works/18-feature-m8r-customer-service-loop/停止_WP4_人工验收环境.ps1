param(
    [string]$RuntimeRoot = "F:\CodexProjects\yunpai-ecommerce-agent-m8r-runtime"
)

$ErrorActionPreference = "Stop"
$statePath = Join-Path $RuntimeRoot "wp4-manual-current.json"
if (-not (Test-Path -LiteralPath $statePath)) {
    Write-Host "No active WP4 manual acceptance state was found."
    exit 0
}

$state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
$process = Get-Process -Id ([int]$state.process_id) -ErrorAction SilentlyContinue
if ($process) {
    Stop-Process -Id $process.Id -Force
    Write-Host "Stopped WP4 manual acceptance server process $($process.Id)."
} else {
    Write-Host "WP4 manual acceptance server was already stopped."
}
Remove-Item -LiteralPath $statePath -Force
Write-Host "Evidence remains in: $($state.evidence_dir)"
