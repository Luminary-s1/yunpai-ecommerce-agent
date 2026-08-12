from ecommerce_agent.database import Database
from ecommerce_agent.knowledge_seed import seed_records
from ecommerce_agent.rag import KnowledgeBase


def test_seed_and_retrieval(tmp_path) -> None:
    db = Database(tmp_path / "rag.sqlite3")
    db.initialize()
    knowledge = KnowledgeBase(db)
    inserted = knowledge.seed_if_empty(seed_records())
    assert inserted >= 150
    assert knowledge.count_active() >= 150

    results = knowledge.retrieve("退货运费谁承担", top_k=5, min_score=0.05, intent="return_exchange")
    assert results
    assert results[0]["intent"] == "return_exchange"
    assert "运费" in results[0]["answer"]


def test_seed_is_idempotent(tmp_path) -> None:
    db = Database(tmp_path / "rag.sqlite3")
    db.initialize()
    knowledge = KnowledgeBase(db)
    assert knowledge.seed_if_empty(seed_records()) >= 150
    assert knowledge.seed_if_empty(seed_records()) == 0


def test_synonym_expansion_matches_warranty_docs(tmp_path) -> None:
    """P2-1 同义词：问法用"保修"，命中只写"质保"的文档。

    种子文档第 55 行 question 含"保修多久"，answer/keywords 用"质保"。
    在无同义词扩展时，"保修"与"质保"无字面 n-gram 交集，检索会漏。
    断言命中即锁定同义词表生效。
    """
    from ecommerce_agent.text_utils import expand_synonyms

    # 单元层：保修 ↔ 质保 双向等价
    assert "质保" in expand_synonyms("保修")
    assert "保修" in expand_synonyms("质保")

    # 检索层：问"质保多久"（文档用词是"保修多久"），应命中保修文档
    db = Database(tmp_path / "rag.sqlite3")
    db.initialize()
    knowledge = KnowledgeBase(db)
    knowledge.seed_if_empty(seed_records())
    results = knowledge.retrieve("质保多久", top_k=5, min_score=0.02, intent="product")
    assert results, "同义词扩展应让'质保'命中'保修'文档"
    assert "保修" in results[0]["question"] or "质保" in results[0]["answer"]


def test_synonym_expansion_does_not_substring_blast(tmp_path) -> None:
    """P2-1 同义词：不做子串盲扩——"发货量"不应被"发货"误扩。

    种子中无"发货量"文档，但若把"发货"子串盲扩进"发货量"，
    会意外把查询词换成含"发货"的原文，造成虚假命中风险。
    本测试锁定 expand_synonyms_text 仅展开完整命中词。
    """
    from ecommerce_agent.text_utils import expand_synonyms_text

    # "发货"组含 寄出/寄送/物流发出；"发货量"是独立词，不应整体被替换
    expanded = expand_synonyms_text("请帮我查询发货量")
    assert "发货量" in expanded
    # 完整命中词"发货"确实会展开出等价表达
    assert "寄出" in expand_synonyms_text("请问什么时候发货")

