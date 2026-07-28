#!/usr/bin/env bash
# macOS/Linux 版本，等价于 scripts/start-glm-coding-test.ps1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

export DATA_DIR="$PROJECT_ROOT/tmp/runtime-0.22.1"
export ADMIN_AUTH_REQUIRED="false"
export ADMIN_API_KEY="test-admin-key-123456"
export BOOTSTRAP_ADMIN_ID="admin-test"
export AUTH_REQUIRED="true"
export MODEL_PROVIDER="glm"
export MODEL_BASE_URL="https://open.bigmodel.cn/api/coding/paas/v4"
export MODEL_NAME="glm-4.7"
export MODEL_ENABLED="true"
export MODEL_MOCK_MODE="false"
export MODEL_ALLOW_CODING_PLAN="true"
export MODEL_STREAMING="false"
export MODEL_THINKING_ENABLED="false"
export RAG_DIRECT_APPROVED_ANSWER="false"
export MODEL_RETRY_ATTEMPTS="1"
export BOOTSTRAP_TENANT_ID="tenant-test"
export BOOTSTRAP_CLIENT_ID="client-test"
export BOOTSTRAP_CLIENT_KEY="test-client-key-12345"
export SUBJECT_HASH_KEY="test-subject-hash-key-12345"
export BOOTSTRAP_CLIENT_CAN_SUPPLY_ORDER_CONTEXT="true"
export CUSTOMER_TEST_ENABLED="true"

if [ -z "${MODEL_API_KEY:-}" ]; then
  echo "MODEL_API_KEY is not configured in the current process environment." >&2
  exit 1
fi

PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  echo "未找到虚拟环境解释器: $PYTHON_BIN（请先创建 .venv，见 README 快速启动）" >&2
  exit 1
fi

cd "$PROJECT_ROOT"
exec "$PYTHON_BIN" -m ecommerce_agent.cli serve --host 127.0.0.1 --port 8104
