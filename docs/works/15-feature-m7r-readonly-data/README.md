# M7-R WP5 只读经营数据验收归档

日期：2026-08-20
功能：F-323

## 收口结论

M7-R WP1～WP4 已在固定代码对象上通过 WP5 代码级、本机技术 Gate。PR #20 已合入
`main`，并在精确 merge tip 上完成复验。该结论只覆盖已经验证的只读数据契约、报表规范化、
商品身份、准备度 API/页面和隔离 Demo，不构成真实平台或生产放行。

## 验收对象与合入拓扑

WP1 是 M7-R 以及 M8-R～M10-R 共用的导入、隐私、来源和证据基建。为解除后续工作包阻塞，
WP1 先行合入 `main`；PR #20 因而只承载 WP2～WP4 的差异。WP5 验收的是两部分组成的
WP1～WP4 集成态，而不是把 WP1 排除在外。

| 范围 | 固定对象 | 状态 |
|---|---|---|
| WP1 公共基建 | `0b54a2475a0b152583a47c4c4ebedffca8293a23`；集成文档 `e127c397f7e291248990b0006ca4876d7e20a075` | 已在 PR base 的祖先链中 |
| PR #20 base | `48013b1d3b29a810288c32f73df028c69070064c` | 已包含 WP1 |
| WP2～WP4 验收 head | `ece61e14fb9c326b38dcde084513494147c508e8` | 缪海南独立验收对象 |
| `main` merge tip | `f6bb47c2dd62ff6798cbcd51d5120ad4ee9b768f` | 2026-08-20 10:31:20 +08:00 合入 |

PR：<https://github.com/a1024053774/yunpai-ecommerce-agent/pull/20>

## 报告与独立性

| 证据 | 角色 | SHA-256 |
|---|---|---|
| [M7R_WP5_ACCEPTANCE_REPORT_20260819.md](M7R_WP5_ACCEPTANCE_REPORT_20260819.md) | 缪海南出具的原独立验收报告；保持原文，不改写、不代签 | `007edc5001f39e4aba26e6361b75152625a52a7f61ac5281699d33e5580aa794` |
| [M7R_WP5_ACCEPTANCE_REPORT_20260819_SUPPLEMENTED.md](M7R_WP5_ACCEPTANCE_REPORT_20260819_SUPPLEMENTED.md) | 实施方补齐精确对象、验收矩阵、命令、mutation 和浏览器证据；不替代独立签署 | `458a743b1a450f8ec2b4488557cbca853611e51052fcfdb911fae63fc990d19e` |

用户转交的第二轮独立 double-check 又核对了两份报告哈希、PR/提交/树与祖先关系，在 detached
快照复跑聚焦和全量测试，独立复现 mutation 红→绿，并逐张核对截图内容与哈希；所有可验证
声称均通过。该 double-check 是复核证据，不冒充缪海南新增签名。

## Merge-tip 复验

在干净 detached 的 `f6bb47c2dd62ff6798cbcd51d5120ad4ee9b768f` 上执行：

- WP1～WP4 聚焦：`104 passed in 16.74s`。
- 仓库全量：`1035 passed, 24 warnings in 421.29s`，无 failed/skipped/xfailed。
- 24 条 warning 均为存量 `traffic_lab_api.py` FastAPI Duplicate Operation ID。
- `compileall`、`git diff --check 48013b1..HEAD`、admin-console 内联 JavaScript
  `node --check` 和 `project-to-act --validate` 均通过。
- WP1 功能提交和 PR #20 head 均验证为 merge tip 的祖先。

## 浏览器证据

| 文件 | 内容 | SHA-256 |
|---|---|---|
| [01-desktop-empty-operational-1280x720.png](../../screenshots/m7r-wp5-20260819/01-desktop-empty-operational-1280x720.png) | 空库 operational：八域 missing、manifest 0、四项缺口保留 | `860e01ba853d1f31fe86c9bea3bb8bb1aebc09096b66f32b3cc106a17dee8682` |
| [02-desktop-demo-1280x720.png](../../screenshots/m7r-wp5-20260819/02-desktop-demo-1280x720.png) | 显式 Demo：八域 8/8、manifest 8、缺口不伪造为已具备 | `81fdac7fb9585e1aa55e3515f4a8f4809a2af5ccaa2506f82d9b97ba843cdbb3` |
| [03-narrow-demo-390x844.png](../../screenshots/m7r-wp5-20260819/03-narrow-demo-390x844.png) | 390×844 窄屏：关键控件可见，表格在面板内滚动 | `4b65ce845dc5089ec9b70d380e40580d58523a0a03b568d2381ad393224bcdcd` |

为保持验收报告登记的原始哈希，三份截图按收到时的文件名和字节归档，不做转码。源文件虽以
`.png` 命名，实际载荷为 JPEG；桌面文件为 1265×712，对应报告中的 `clientWidth=1265`，
窄屏文件为 375×812，对应 `clientWidth=375`。文件名中的 1280×720 / 390×844 是浏览器
viewport 口径，不是归档文件的像素编码声明。

## 已签署范围与未放行事项

已关闭的是 F-323 的代码级、本机里程碑：WP1 统一契约、WP2 通用报表适配、WP3 商品身份与
对账、WP4 数据准备度与隔离 Demo 已合入并通过 WP5。对应证据为 E-20260820-001，Gate 为
G-M7R-WP5-001 与 G-M7R-ALL-001。

以下事项仍未放行：

- 未经授权的真实淘宝/天猫等平台接入与真实平台字段全覆盖。
- 基于真实经营数据的业务结论。
- 生产发布、生产权限、平台写能力或自动经营动作。
- M8-R～M10-R 的实现、验收或生产放行。
