#!/bin/bash
# 云湃 M3 全量回归测试固定流程（Task 10 专用，可重复执行）
# 用法: bash scripts/full_regression.sh
# 输出: /d/claude-checkpoints/fulltest-<timestamp>.log + 退出码
set -euo pipefail

REPO=/d/yunpai-ecommerce-agent
OUT_DIR=/d/claude-checkpoints
STAMP=$(date +%Y%m%d-%H%M%S)
LOG="$OUT_DIR/fulltest-$STAMP.log"

echo "=== 全量回归开始 $(date) ===" | tee "$LOG"
echo "仓库: $REPO | 分支: $(cd $REPO && git branch --show-current)" | tee -a "$LOG"
echo "HEAD: $(cd $REPO && git log --oneline -1)" | tee -a "$LOG"

cd "$REPO"

# 1. 工作区必须干净（未提交改动 = 测试的不是完整提交态）
DIRTY=$(git status --short | wc -l)
echo "工作区未提交文件数: $DIRTY" | tee -a "$LOG"
if [ "$DIRTY" -gt 0 ]; then
  echo "!! 工作区有未提交改动，先 commit 再测" | tee -a "$LOG"
  exit 2
fi

# 2. 全量 pytest
echo "=== 全量 pytest 开始 $(date) ===" | tee -a "$LOG"
PYTHONPATH=src python -m pytest tests/ -q --no-header -p no:cacheprovider 2>&1 | tee -a "$LOG"
STATUS=${PIPESTATUS[0]}
echo "=== 全量 pytest 结束 $(date) exit=$STATUS ===" | tee -a "$LOG"

# 3. 结果汇总
echo "=== 汇总 ===" | tee -a "$LOG"
tail -3 "$LOG" | tee -a "$LOG"
echo "日志: $LOG" | tee -a "$LOG"
exit $STATUS
