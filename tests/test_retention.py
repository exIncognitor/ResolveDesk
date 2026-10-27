import pytest

from resolvedesk.monitoring import Monitor
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.retention import Retention
from resolvedesk.store import Principal,ReceiptExpired
from test_turn_receipts import store,postgres_store,USER


def test_preview_then_synthetic_retention_preserves_idempotency_and_active_work(store):
    HybridKnowledge(store)
    Monitor(store)
    run,_=store.create_run(USER,"old","old-key")
    store.claim(USER,run["id"]);store.checkpoint(USER,run["id"],{"test":"checkpoint"})
    store.finish(USER,run["id"],"completed","answer")
    active,_=store.create_run(USER,"still active","active-key")
    with store.db(True) as db:
        db.execute("UPDATE runs SET updated=1")
        db.execute("UPDATE events SET created=1")
        db.execute("UPDATE turn_receipts SET updated=1")
    retention=Retention(store)
    support=Principal(USER.tenant,"operator","support")
    preview=retention.sweep(support,days=7)
    assert preview["counts"]["receipt_responses"]==1 and not preview["applied"]
    assert store.get_turn(USER,run["id"],"old-key")["answer"]=="answer"
    with pytest.raises(PermissionError):retention.sweep(USER,days=7,apply=True)
    with pytest.raises(ValueError):retention.sweep(support,days=1,apply=True)
    result=retention.sweep(support,days=7,apply=True)
    assert result["applied"] and result["counts"]==preview["counts"]
    with pytest.raises(ReceiptExpired):store.create_run(USER,"old","old-key")
    assert len(store.history(USER,run["id"]))==2
    assert store.get_run(USER,active["id"])["status"]=="queued" and store.events(USER,active["id"])
    next_turn,new=store.reply(USER,run["id"],"new reply","new-reply-key")
    assert new and next_turn["status"]=="queued"
    assert not retention.sweep(support,days=7)["counts"]["receipt_responses"]
