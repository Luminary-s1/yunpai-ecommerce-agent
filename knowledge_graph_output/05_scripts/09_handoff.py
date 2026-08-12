"""09_handoff.py — 生成任务6交接包（07_handoff/）。

对齐任务6交接内容：清洗后的知识库源数据集（结构化文档 + 数据字典）。
同时附带下游复用物（六类实体/五类关系/CSV/Cypher/真值表）供任务4/任务2承接。

盲点5（D-035）：末尾固化 zip 归档（07_handoff.zip），不再手工压缩。
注意：07_handoff.zip 已在 .gitignore 排除（不入库），交付时在本地分发。
"""
from __future__ import annotations

import shutil
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HANDOFF = ROOT / "07_handoff"
ZIP_PATH = ROOT / "07_handoff.zip"

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
        "| 品类 Category | 10 | BELONGS_TO（属于） | 19 |",
        "| 商品 Product(SPU) | 8 | HAS_ATTR（具有） | 51 |",
        "| 商品 SKU | 12 | APPLIES_TO（适用） | 36 |",
        "| 属性 Attribute | 51 | REFERS_TO（引用） | 65 |",
        "| 售后政策 Policy | 9 | RELATED_TO（关联） | 69 |",
        "| 客服话术 Script | 52 | | |",
        "| 常见问答 FAQ | 63 | | |",
        "| 行业规则 Rule | 17 | | |",
        "| **合计** | **222** | **合计** | **240** |",
        "",
        "> 注：数字以 `02_clean/clean_manifest.json`（由 `05_scripts/11_refresh_manifest.py` 重写）为准。",
        "",
        "## 下游复用（任务4/任务2 输入）",
        "",
        "- **任务4 实体抽取输入**：`02_clean/*.json`（六类实体已按 Schema 构建，含置信度）",
        "- **任务4 抽检输入**：`06_report/truth_table.csv`（覆盖分母 171：SPU 8 + SKU 12 + 品类 10 + 政策 9 + FAQ 63 + 话术 52 + 规则 17）+ `sampling_plan.csv`（核心池 60 条，人工标注模板见 `sampling_review_instructions.md`）",
        "- **任务2 导入输入**：`04_import/`（nodes/rels CSV + 00_setup / 01_load_nodes / 02_load_rels Cypher）",
        "- **任务3 Wiki 输入**：`02_clean/*.md`（五份可读文档）+ `03_dictionary/`（分类契约）",
        "",
        "## 遗留问题",
        "",
        "- S10 客服话术范本为台湾繁体，仅作参考方向，未并入标准话术库",
        "- 新增品类保修口径为人工构造，建议后续以真实品牌政策核对",
        "- M3 已实际完成：Neo4j 导入验证（`04_import/README.md` docker-compose + Cypher）、人工抽检模板已生成（`sampling_plan.csv` 60 条待标注，统计脚本 `13_sampling_report.py`）、检索评测 35 题（`06_report/retrieval_evaluation_report.json`）",
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


def make_zip() -> Path:
    """固化 zip 归档：07_handoff/ → 07_handoff.zip（不入 git，本地分发用）。"""
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(HANDOFF.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(ROOT))
    print(f"✓ 已归档 {ZIP_PATH}")
    return ZIP_PATH


def main() -> None:
    HANDOFF.mkdir(parents=True, exist_ok=True)
    copy_dirs()
    gen_readme()
    print("✓ README.md")
    make_zip()
    print(f"\n交接包生成完成：{HANDOFF}")


if __name__ == "__main__":
    main()
