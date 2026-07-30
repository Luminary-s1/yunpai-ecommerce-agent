# 本次合并工作索引

本目录只记录本次由个人分支合并到 fork `main` 的工作，不追溯此前历史。上游 `upstream/main` 未执行合并或推送；F-107 与 F-109 明确排除。

[浏览 11 项真实运行截图](evidence.html)。每个子目录也各自保存一张 `verification.png`；图片均为真实 PNG 数据，不再使用仅复述测试结论的摘要卡片作为验证证据。

## 合并顺序

| 顺序 | 类型 | 来源 | 合并提交 | 验证与操作文档 |
|---:|---|---|---|---|
| 1 | fix | PR #3 `fix/llm-stream-error-handling` | `2f30bac` | [LLM 流式错误处理](01-fix-llm-stream-error-handling/README.md) |
| 2 | feature | PR #5 `feature/f101-channel-adapter-sdk` | `20338bf` | [F-101 渠道适配器 SDK](02-feature-f101-channel-adapter-sdk/README.md) |
| 3 | docs | PR #1 `docs/macos-quickstart` | `a7336de` | [macOS/Linux 快速启动](03-docs-macos-quickstart/README.md) |
| 4 | feature | PR #4 `feature/recurring-operator-shifts` | `c381409` | [周期批量排班](04-feature-recurring-operator-shifts/README.md) |
| 5 | feature | PR #6 `feature/order-handoff-visibility` | `26d9bed` | [订单人工客服状态](05-feature-order-handoff-visibility/README.md) |
| 6 | fix | `fix/finance-expense-truncation` | `34650db` | [利润费用截断修复](06-fix-finance-expense-truncation/README.md) |
| 7 | feature | `feature/f103-channel-context-envelope` | `9a226f5` | [F-103 统一渠道信封](07-feature-f103-channel-context-envelope/README.md) |
| 8 | feature | `feature/f104-knowledge-gray-release` | `4c41894` | [F-104 知识灰度发布](08-feature-f104-knowledge-gray-release/README.md) |
| 9 | feature | `feature/f105-sop-gray` | `a8981f9` | [F-105 SOP 灰度发布](09-feature-f105-sop-gray/README.md) |
| 10 | feature | `feature/f106-product-advisor` | `1a3dfa3` | [F-106 商品顾问](10-feature-f106-product-advisor/README.md) |
| 11 | feature | `feature/f106-fuzzy-product-lookup` | `8a8f9b7` | [F-106 商品模糊检索](11-feature-f106-fuzzy-product-lookup/README.md) |

## 开发完成但未合并

下表的工作已完成开发与验收，但按要求保持未提交、未合并，因此不计入上面的合并顺序，也不在 `evidence.html` 中。

| 类型 | 来源 | 状态 | 验证与操作文档 |
|---|---|---|---|
| feature | `feature/m5-operations-assistant` | 开发与验收完成，未提交 | [M5 运营辅助与文案生成](12-feature-m5-operations-assistant/README.md) |

## 本次统一验收

在合并后的 `main` 上执行了覆盖 LLM、渠道、排班、订单、财务、知识、SOP、商品顾问、模糊检索与后台契约的定向集成矩阵：

```bash
NO_PROXY=127.0.0.1,localhost \
no_proxy=127.0.0.1,localhost \
ALL_PROXY=http://127.0.0.1:9 \
HTTP_PROXY=http://127.0.0.1:9 \
HTTPS_PROXY=http://127.0.0.1:9 \
.venv/bin/python -m pytest -q \
  tests/test_llm.py \
  tests/test_react_graph.py \
  tests/test_channel_sdk_contract.py \
  tests/test_channel_sdk_runtime.py \
  tests/test_handoff_dispatch.py \
  tests/test_order_handoff_visibility.py \
  tests/test_marketing_finance_api.py \
  tests/test_knowledge_rollout.py \
  tests/test_sop_rollout.py \
  tests/test_product_advisor.py \
  tests/test_catalog_orders_metrics.py \
  tests/test_admin_console.py
```

结果：`100 passed in 102.20s`。

随后在同一合并结果上执行完整测试套件：

```bash
NO_PROXY=127.0.0.1,localhost \
no_proxy=127.0.0.1,localhost \
ALL_PROXY=http://127.0.0.1:9 \
HTTP_PROXY=http://127.0.0.1:9 \
HTTPS_PROXY=http://127.0.0.1:9 \
.venv/bin/python -m pytest -q
```

结果：`302 passed in 359.42s`，退出码 `0`。

## 截图补强后复验

将 9 张摘要卡片替换为真实终端、Swagger 或后台页面截图，并把全部 11 张 `verification.png` 转换为真实 PNG 后，再次执行上述完整测试套件。

结果：`302 passed in 520.74s`，退出码 `0`。证据总页经本地 HTTP 服务加载，11 张图片请求均返回 `200`。

## 明确未合并

- `feature/f107-*`：未合并，祖先检查返回非祖先。
- `feature/f109-*`：未合并，祖先检查返回非祖先。
- 所有 upstream PR 保持原状态，本次只更新个人 fork。
