$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$env:DATA_DIR = Join-Path $projectRoot "tmp\runtime-0.22.1"
$env:ADMIN_AUTH_REQUIRED = "false"
$env:ADMIN_API_KEY = "test-admin-key-123456"
$env:BOOTSTRAP_ADMIN_ID = "admin-test"
$env:AUTH_REQUIRED = "true"
$env:MODEL_PROVIDER = "glm"
$env:MODEL_BASE_URL = "https://open.bigmodel.cn/api/coding/paas/v4"
$env:MODEL_NAME = "glm-4.7"
if (-not $env:MODEL_API_KEY) {
    $env:MODEL_API_KEY = [Environment]::GetEnvironmentVariable("MODEL_API_KEY", "User")
}
$env:MODEL_ENABLED = "true"
$env:MODEL_MOCK_MODE = "false"
$env:MODEL_ALLOW_CODING_PLAN = "true"
$env:MODEL_STREAMING = "false"
$env:MODEL_THINKING_ENABLED = "false"
$env:RAG_DIRECT_APPROVED_ANSWER = "false"
$env:MODEL_RETRY_ATTEMPTS = "1"
$env:BOOTSTRAP_TENANT_ID = "tenant-test"
$env:BOOTSTRAP_CLIENT_ID = "client-test"
$env:BOOTSTRAP_CLIENT_KEY = "test-client-key-12345"
$env:SUBJECT_HASH_KEY = "test-subject-hash-key-12345"
$env:BOOTSTRAP_CLIENT_CAN_SUPPLY_ORDER_CONTEXT = "true"
$env:CUSTOMER_TEST_ENABLED = "true"

if (-not $env:MODEL_API_KEY) {
    throw "MODEL_API_KEY is not configured in the current process environment."
}

Set-Location $projectRoot
py -3.12 -m ecommerce_agent.cli serve --host 127.0.0.1 --port 8104
