"""09_handoff.py — 生成任务6交接包（07_handoff/）。

对齐任务6交接内容：清洗后的知识库源数据集（结构化文档 + 数据字典）。
同时附带下游复用物（六类实体/五类关系/CSV/Cypher/真值表）供任务4/任务2承接。
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HANDOFF = ROOT / "07_handoff"

TODAY = datetime.now().strftime("%Y-%m-%d")


def gen_readme() -> Path:
    """交接包说明文档。"""
    lines = [
        "# 任务6 交接包：知识库数据采集与清洗加工",
        "",
        f"- 交接日期：{TODAY}",
        "- 承接任务：任务6（知识库数据采集与清洗加工）",
        "- 交付范围：清洗后的知识库源数据集（结构化文档 + 数据字典）",
        "",
        "## 目录结构",
        "",
        "```",
        "07_handoff/",
        "├─ README.md",
        "├─ 01_raw/          原始素材（种子/知识源/网络/人工）",
        "├─ 02_clean/        清洗后结构化数据（六类实体+五类关系 JSON + Markdown）",
        "├─ 03_dictionary/   数据字典 + 机器可读契约",
        "├─ 04_import/       Neo4j 导入文件（CSV + Cypher）",
        "└─ 06_report/       校验/覆盖/格式/图谱统计 报告",
        "```",
        "",
        "## 验收对照（任务6）",
        "",
        "| 验收标准 | 落实 | 证据 |",
        "|---|---|---|",
        "| 知识库数据覆盖客服、运营核心场景 | 19 个客服场景全覆盖 | `06_report/scene_coverage.md` |",
        "| 数据格式规范统一 | 26 项格式复核全过 | `06_report/format_review.md` |",
        "",
        "## 数据概览",
        "",
        "| 实体 | 条数 | 关系 | 条数 |",
        "|---|---|---|---|",
        "| 品类 Category | 10 | BELONGS_TO（属于） | 12 |",
        "| 商品 Product(SPU) | 8 | HAS_ATTR（具有） | 51 |",
        "| 商品 SKU | 12 | APPLIES_TO（适用） | 34 |",
        "| 属性 Attribute | 51 | REFERS_TO（引用） | 64 |",
        "| 售后政策 Policy | 9 | RELATED_TO（关联） | 52 |",
        "| 客服话术 Script | 52 | | |",
        "| 常见问答 FAQ | 60 | | |",
        "| 行业规则 Rule | 9 | | |",
        "| **合计** | **211** | **合计** | **213** |",
        "",
        "## 下游复用（任务4/任务2 输入）",
        "",
        "- **任务4 实体抽取输入**：`02_clean/*.json`（六类实体已按 Schema 构建，含置信度）",
        "- **任务4 抽检输入**：`06_report/truth_table.csv`（核心实体分母 39）+ `sampling_plan.csv`（核心池 60 条）",
        "- **任务2 导入输入**：`04_import/`（nodes/rels CSV + 00_setup / 01_load_nodes / 02_load_rels Cypher）",
        "- **任务3 Wiki 输入**：`02_clean/*.md`（五份可读文档）+ `03_dictionary/`（分类契约）",
        "",
        "## 遗留问题",
        "",
        "- S10 客服话术范本为台湾繁体，仅作参考方向，未并入标准话术库",
        "- 新增品类保修口径为人工构造，建议后续以真实品牌政策核对",
        "- M3（Neo4j 导入验证）与 M4（人工抽检）待执行",
        "",
    ]
    readme = HANDOFF / "README.md"
    readme.write_text("\n".join(lines), encoding="utf-8")
    return readme


def copy_dirs() -> None:
    """复制各目录到交接包。"""
    for name in ["01_raw", "02_clean", "03_dictionary", "04_import", "06_report"]:
        src = ROOT / name
        dst = HANDOFF / name
        if dst.exists():
            shutil.rmtree(dst)
        if src.exists():
            shutil.copytree(src, dst)
            print(f"✓ 复制 {name}/")


def main() -> None:
    HANDOFF.mkdir(parents=True, exist_ok=True)
    copy_dirs()
    gen_readme()
    print("✓ README.md")
    print(f"\n交接包生成完成：{HANDOFF}")


if __name__ == "__main__":
    main()
