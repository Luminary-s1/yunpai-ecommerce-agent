# Codex repository instructions

This file applies to the repository root and every descendant directory. Treat the
rules below as project requirements, not suggestions.

## Required reading

Before implementation, refactoring, or code review, read the relevant source of
truth instead of inferring policy from existing code:

- Always read `CONTRIBUTING.md` sections 5, 9, 10, and 11.
- Use `.project-to-act/PROJECT_OVERVIEW.md` for stable architecture decisions,
  especially D-034 and D-035. Do not use its dated history as current state.
- For changes involving agent routing, intent, LangGraph nodes, prompts, policy,
  RAG, customer copy, streaming chat, mocks, evaluation, or release gates, also
  read `docs/AUDIT_ROUTING_EVOLVABILITY_20260807.md`.
- For M5-R or M6-R work, read the matching workbench under `docs/tasks/` and its
  "实现纪律（执行者必读）" section before editing code.

The audit documents existing debt. It does not authorize unrelated cleanup in a
scoped task.

## D-034: preserve model semantic authority

The governing boundary is:

> Deterministic code decides whether an action can be executed safely now. The
> model decides what the user wants and what semantic step should happen next.

- Do not use keywords, regular expressions, classifier labels, retrieval scores,
  or `route_reason` strings to choose a semantic route, bypass model deliberation,
  override an `answer`/`clarify`/`observe` decision, or select evidence-bearing
  customer copy.
- Intent classifiers and keyword matches are signals only. They may be supplied to
  the model as non-authoritative context, used as retrieval/risk features, and
  recorded for audit. They must not become execution authority.
- A gate may move a model decision in a safer direction only because of verifiable
  execution facts: authentication or scope failure, schema validation, missing
  trusted context, tool/SOP policy, idempotency, postcondition failure, shadow-mode
  write suppression, context conflict, or step-budget exhaustion.
- Keep deterministic boundaries for prompt-injection refusal, authorization,
  tenant/store/order isolation, typed tool validation, idempotency, postcondition
  verification, release gates, and audited human handoff.
- A model-bypassing answer fast path is allowed only for human-approved immutable
  content whose normalized question exactly matches the request. A high retrieval
  score alone is not sufficient.
- `/v1/chat` and `/v1/chat/stream` must reuse the same decision and generation
  semantics. Do not copy graph-node routing or generation logic into the streaming
  service path.
- Mocks must be fixed or table-driven test doubles. Do not reproduce production
  semantic routing with a second keyword tree.
- "The model must not be called" assertions are limited to model-disabled mode,
  deterministic security refusal, and verified identity/scope conflicts. Do not
  lock ordinary semantic paths to model bypasses.
- Before changing the existing complaint policy or product-answer fast path, make
  the behavior choice explicit: complaint handoff versus priority-only signaling,
  and exact approved match versus broader retrieval-based reuse.

## D-035: keep the project evolvable

- Define each shared enum, schema version, field list, threshold, and registry in
  one authoritative place. If duplication is unavoidable, add generation or a
  cross-check that compares the copies.
- Before adding a migration function, validator entry, or registry key, search for
  duplicate function names, method names, and dictionary keys; Python may silently
  overwrite them.
- Tests must assert their own increment or invariant with membership and bounds.
  Do not freeze global counts, complete topology snapshots, exact global versions,
  or the absence of unrelated future members.
- Versioned fields need a read-side compatibility, migration, or rejection path.
  A version that is only written is not protection.
- Schema changes must update the authoritative reservation table in
  `CONTRIBUTING.md`, review `_validate_schema`, cover supported upgrade boundaries,
  and state the backup-manifest compatibility strategy.
- Current-state documentation must point to an authoritative runtime/config source
  instead of copying mutable schema, test, scenario, or module counts. Historical
  evidence must include its date and commit.

## Verification and change discipline

- Preserve user-owned dirty-worktree changes and keep edits scoped to the request.
- Use the project environment (`.venv/bin/python -m pytest`) for Python tests.
- For routing changes, add counterexamples for negation, hypothetical/presale
  questions, compound requests, and model decisions that must survive the gate.
  Run the real-model intent benchmark when credentials and authorization are
  available; report mock-only evidence separately.
- Do not edit `CONTRIBUTING.md` outside `main` unless the user explicitly authorizes
  it. Existing uncommitted changes belong to the user and must not be reverted.

## M3 知识库：Neo4j 部署与数据契约

- **Neo4j 是核心能力但可替换**：生产回答链路（客服 RAG）走运行时 SQLite 表
  （`src/ecommerce_agent/rag.py`），不依赖 Neo4j；Neo4j 只服务图谱检索/多跳/可视化。
- **部署**：`docker compose up -d`（neo4j:5-community），或按
  `knowledge_graph_output/04_import/README.md` 手动部署。导入 = 3 个 Cypher（幂等 MERGE）。
- **连接参数走 env**：`NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD`
  （config.Settings.neo4j_*）。默认密码是占位符 `change-me`，**禁止把真实密码提交仓库**。
- **测试不依赖本机 Neo4j**：`tests/conftest.py` 的 `mock_neo4j_query` / `mock_neo4j_transport`
  fixture 用 `unittest.mock` 打桩，独立环境全绿。
- **数据单一事实源（D-035）**：根目录 `02_clean/` 是唯一事实源；
  `07_handoff/` 与 zip 由 `09_handoff.py` 再生成；`verify_single_source.py` 一键校验一致性；
  数据修正后必须重跑 `06_export → 10_extend_rels → 11_refresh_manifest → 09_handoff`。
- **禁止盲跑 `03_clean.py` 全管线**：会重建 faq 丢 3 条补充 FAQ、重建关系丢扩充边；
  数据修正走"源头 + 手术"。
- **知识接入生产**：`AgentService` 启动时 `import_to_runtime` 把 02_clean 导入运行时表
  （幂等，`KG_IMPORT_ENABLED=false` 可关，测试默认关）。
