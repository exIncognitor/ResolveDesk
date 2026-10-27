"""Retry, stale worker and cross-worker recovery contracts on both SQL backends."""
from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from resolvedesk.store import Conflict, FencedExecution, Principal, Store
from test_database_backends import postgres_store

USER = Principal("receipt-test", "alice")


@pytest.fixture(params=["sqlite", "postgresql"])
def store(request, tmp_path):
    if request.param == "postgresql": return request.getfixturevalue("postgres_store")
    store = Store(tmp_path/"receipts.sqlite3")
    yield_store = store
    request.addfinalizer(yield_store.close)
    return store


def completed(store):
    run,_ = store.create_run(USER,"最初问题","create-key")
    store.claim(USER,run["id"])
    store.finish(USER,run["id"],"completed","最初答案")
    return run["id"]


def test_retry_replays_the_original_turn_even_after_newer_turn(store):
    run_id = completed(store)
    first,new = store.reply(USER,run_id,"第一个追问","reply-key")
    assert new and not first["answer"]
    store.claim(USER,run_id);store.finish(USER,run_id,"completed","追问答案")
    saved = store.get_turn(USER,run_id,"reply-key")
    store.reply(USER,run_id,"第二个追问","other-key")
    store.claim(USER,run_id);store.finish(USER,run_id,"completed","更新的答案")
    replay,new = store.reply(USER,run_id,"第一个追问","reply-key")
    assert not new and replay == saved and replay["answer"] == "追问答案"
    original,new = store.create_run(USER,"最初问题","create-key")
    assert not new and original["answer"] == "最初答案"
    assert original["event_end"] < replay["event_cursor"]
    with pytest.raises(Conflict): store.reply(USER,run_id,"篡改输入","reply-key")
    for other in (Principal("other","alice"),Principal(USER.tenant,"bob","support")):
        with pytest.raises(KeyError): store.get_turn(other,run_id,"reply-key")


def test_racing_reply_creates_one_user_message(store):
    run_id = completed(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _:store.reply(USER,run_id,"同一个追问","same-reply"),range(8)))
    assert sum(new for _,new in results) == 1
    assert sum(m["role"]=="user" for m in store.history(USER,run_id)) == 2


def test_expired_and_superseded_worker_cannot_checkpoint_or_finish(store):
    run,_ = store.create_run(USER,"工作","fencing-key")
    old = store.claim_execution(USER,run["id"],"worker-old",60)
    with store.db(True) as db: db.execute("UPDATE runs SET lease_until=? WHERE id=?",(time.time()-1,run["id"]))
    with store.execution(old):
        with pytest.raises(FencedExecution): store.checkpoint(USER,run["id"],{"stale":True})
        with pytest.raises(FencedExecution): store.finish(USER,run["id"],"completed","过期结果")
    assert not store.renew_execution(USER,old)
    assert store.recover_expired() == 1
    store.resume(USER,run["id"])
    new = store.claim_execution(USER,run["id"],"worker-new",60)
    assert new.epoch == old.epoch+1
    with store.execution(old):
        with pytest.raises(FencedExecution): store.event(USER,run["id"],"stale",{})
    assert not store.lose_execution(USER,old)
    with store.execution(new): store.finish(USER,run["id"],"completed","新结果")
    assert store.get_run(USER,run["id"])["answer"] == "新结果"


def test_recovery_keeps_another_live_worker_and_lease_loss_pauses_once(store):
    run,_ = store.create_run(USER,"工作","live-worker")
    token = store.claim_execution(USER,run["id"],"live-owner",60)
    assert store.recover_expired() == 0
    assert store.renew_execution(USER,token)
    assert store.lose_execution(USER,token)
    assert not store.lose_execution(USER,token)
    assert store.get_turn(USER,run["id"],"live-worker")["status"] == "paused"
