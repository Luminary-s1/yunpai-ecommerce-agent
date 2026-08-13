#!/bin/bash
# 云湃 M3 知识库模块测试固定流程（用户确认：只测 M3，不跑全量）
# 用法: bash scripts/m3_regression.sh
# 输出: /d/claude-checkpoints/m3test-<timestamp>.log + 退出码
set -uo pipefail

REPO=/d/yunpai-ecommerce-agent
OUT_DIR=/d/claude-checkpoints
STAMP=$(date +%Y%m%d-%H%M%S)
LOG="$OUT_DIR/m3test-$STAMP.log"

# M3 模块测试集（20 个文件：负责人复验 106 套件 + 本轮新增）
M3_TESTS="tests/test_intent_routing_integration.py \
tests/test_service_stream.py \
tests/test_graph_retrieval.py \
tests/test_rag.py \
tests/test_memory_service.py \
tests/test_context_budget.py \
tests/test_evaluation_suite.py \
tests/test_retrieval_persistence.py \
tests/test_knowledge_engine.py \
tests/test_wiki_api.py \
tests/test_graph_api.py \
tests/test_single_source.py \
tests/test_migrations.py \
tests/test_knowledge_runtime_bridge.py \
tests/test_prompt_templates.py \
tests/test_security_observability.py \
tests/test_neo4j_client.py \
tests/test_intent_guardrails.py \
tests/test_import_assets_api.py \
tests/test_knowledge_rollout.py"

echo "=== M3 模块测试开始 $(date) ===" | tee "$LOG"
echo "仓库: $REPO | 分支: $(cd $REPO && git branch --show-current) | HEAD: $(cd $REPO && git log --oneline -1)" | tee -a "$LOG"

cd "$REPO"

# 1. 工作区必须干净（未提交改动 = 测试的不是完整提交态）
DIRTY=$(git status --short | wc -l)
echo "工作区未提交文件数: $DIRTY" | tee -a "$LOG"
if [ "$DIRTY" -gt 0 ]; then
  echo "!! 工作区有未提交改动，先 commit 再测" | tee -a "$LOG"
  exit 2
fi

# 2. M3 模块 pytest（约 3-5 分钟）
echo "=== M3 pytest 开始 $(date) ===" | tee -a "$LOG"
PYTHONPATH=src python -m pytest $M3_TESTS -q --no-header -p no:cacheprovider 2>&1 | tee -a "$LOG"
STATUS=${PIPESTATUS[0]}
echo "=== M3 pytest 结束 $(date) exit=$STATUS ===" | tee -a "$LOG"

# 3. 结果汇总
tail -3 "$LOG" | tee -a "$LOG"
echo "日志: $LOG" | tee -a "$LOG"
exit $STATUS
