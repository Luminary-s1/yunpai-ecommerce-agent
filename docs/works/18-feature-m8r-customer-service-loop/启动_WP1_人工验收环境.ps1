param(
    [int]$Port = 8091
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "未找到 WP1 开发环境：$python"
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    throw "端口 $Port 已被占用，请先关闭旧服务，或使用 -Port 指定其他端口。"
}

$runtimeRoot = "F:\CodexProjects\yunpai-ecommerce-agent-m8r-runtime"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dataDir = Join-Path $runtimeRoot "wp1-manual-$stamp"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

$env:DATA_DIR = $dataDir
$env:MODEL_ENABLED = "false"
$env:MODEL_MOCK_MODE = "false"
$env:KG_IMPORT_ENABLED = "false"
$env:KG_DREAM_WORKER_ENABLED = "false"
$env:ADMIN_AUTH_REQUIRED = "false"
$env:AUTH_REQUIRED = "false"
$env:BOOTSTRAP_TENANT_ID = "m8r-wp1-manual"
$env:BOOTSTRAP_ADMIN_ID = "xie-liangxuan"
$env:MIN_FREE_DISK_MB = "1"

$environmentInfo = [ordered]@{
    tester = "谢良璇"
    work_package = "M8-R-WP1"
    started_at = [DateTimeOffset]::Now.ToString("o")
    project_root = $projectRoot
    data_dir = $dataDir
    base_url = "http://127.0.0.1:$Port"
    swagger_url = "http://127.0.0.1:$Port/docs"
    model_enabled = $false
    admin_auth_required = $false
    loopback_only = $true
}
$environmentFile = Join-Path $dataDir "人工验收环境.json"
$environmentInfo | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $environmentFile -Encoding UTF8

Write-Host ""
Write-Host "M8-R WP1 人工验收环境即将启动" -ForegroundColor Cyan
Write-Host "项目目录：$projectRoot"
Write-Host "隔离数据：$dataDir"
Write-Host "健康检查：http://127.0.0.1:$Port/health"
Write-Host "接口页面：http://127.0.0.1:$Port/docs"
Write-Host ""
Write-Host "此窗口必须保持打开。验收完成后按 Ctrl+C 停止服务。" -ForegroundColor Yellow
Write-Host "WP1 不需要模型，本环境不会调用外部模型，也不会连接真实店铺。"
Write-Host ""

Set-Location $projectRoot
& $python -m ecommerce_agent.cli serve --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
