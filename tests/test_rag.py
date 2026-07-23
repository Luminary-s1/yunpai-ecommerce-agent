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

