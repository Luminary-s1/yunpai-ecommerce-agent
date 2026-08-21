param(
    [string]$Tester = "谢良璇",
    [string]$RuntimeRoot = "F:\CodexProjects\yunpai-ecommerce-agent-m8r-runtime",
    [int]$Port = 8092
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$serverScript = Join-Path $PSScriptRoot "WP4_人工验收服务器.py"

if (-not (Test-Path -LiteralPath $python)) {
    throw "M8-R Python environment was not found: $python"
}
if (-not (Test-Path -LiteralPath $serverScript)) {
    throw "WP4 manual acceptance server was not found: $serverScript"
}

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    $pids = $existing | Select-Object -ExpandProperty OwningProcess -Unique
    throw "Port $Port is already in use by process: $($pids -join ', ')"
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dataDir = Join-Path $RuntimeRoot "wp4-manual-data\$stamp"
$evidenceDir = Join-Path $RuntimeRoot "wp4-manual-evidence\$stamp"
$stdoutPath = Join-Path $evidenceDir ($Tester + "_WP4人工验收服务输出_" + $stamp + ".txt")
$stderrPath = Join-Path $evidenceDir ($Tester + "_WP4人工验收服务错误_" + $stamp + ".txt")
$statePath = Join-Path $RuntimeRoot "wp4-manual-current.json"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
New-Item -ItemType Directory -Force -Path $evidenceDir | Out-Null

$arguments = @(
    $serverScript,
    "--data-dir", $dataDir,
    "--evidence-dir", $evidenceDir,
    "--host", "127.0.0.1",
    "--port", [string]$Port,
    "--tester", $Tester
)
$server = Start-Process -FilePath $python -ArgumentList $arguments -PassThru `
    -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath `
    -WindowStyle Hidden

$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    Start-Sleep -Seconds 1
    if ($server.HasExited) {
        $errorText = if (Test-Path -LiteralPath $stderrPath) {
            Get-Content -LiteralPath $stderrPath -Raw
        } else { "No error log was produced." }
        throw "WP4 server exited early with code $($server.ExitCode).`n$errorText"
    }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
        if ($health.status -eq "ok") {
            $ready = $true
            break
        }
    } catch {
        # Continue polling while the isolated service initializes.
    }
}
if (-not $ready) {
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    throw "WP4 server did not become ready within 60 seconds. See $stderrPath"
}

$state = [ordered]@{
    tester = $Tester
    work_package = "M8-R-WP4"
    started_at = (Get-Date).ToString("o")
    process_id = $server.Id
    port = $Port
    base_url = "http://127.0.0.1:$Port"
    admin_url = "http://127.0.0.1:$Port/admin"
    data_dir = $dataDir
    evidence_dir = $evidenceDir
    stdout = $stdoutPath
    stderr = $stderrPath
    external_model_called = $false
    platform_write_performed = $false
}
$state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding UTF8

Write-Host "M8-R WP4 manual acceptance environment is ready." -ForegroundColor Cyan
Write-Host "Tester: $Tester"
Write-Host "Admin page: http://127.0.0.1:$Port/admin" -ForegroundColor Green
Write-Host "Data directory: $dataDir"
Write-Host "Evidence directory: $evidenceDir"
Write-Host "Controlled table-driven local model only; no external model or platform write is used."
Write-Host "Open Advanced Admin, then choose '客服影子评审'."
Write-Host "When finished, run:"
Write-Host "& `"$PSScriptRoot\停止_WP4_人工验收环境.ps1`""
