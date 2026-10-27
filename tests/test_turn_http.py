import asyncio
import hashlib
import os

from fastapi.testclient import TestClient
import pytest

from resolvedesk.agent import DeskAgent
from resolvedesk.app import create_app
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.store import Store
from test_resolvedesk import USER,client_with,response
from test_database_backends import postgres_store
from test_redis_coordination import cache


def test_http_requires_reply_key_and_limits_replay_to_original_turn(tmp_path):
    store = Store(tmp_path/"http.sqlite3")
    kb = HybridKnowledge(store)
    agent = DeskAgent(store,kb,client_with())
    agent.schedule = lambda *args:None
    accounts = {hashlib.sha256(b"test-token").hexdigest():vars(USER)}
    with TestClient(create_app(store,kb,agent,accounts)) as client:
        client.headers["Authorization"] = "Bearer test-token"
        run = client.post("/runs",json={"message":"start","request_key":"start-key"}).json()
        store.claim(USER,run["id"]);store.finish(USER,run["id"],"completed","first")
        assert client.post(f"/runs/{run['id']}/reply",json={"message":"reply"}).status_code == 422
        body = {"message":"reply","request_key":"reply-key"}
        created = client.post(f"/runs/{run['id']}/reply",json=body).json()
        assert not created["replayed"]
        store.claim(USER,run["id"]);store.finish(USER,run["id"],"completed","reply answer")
        original = client.get(f"/runs/{run['id']}/turns/reply-key").json()
        store.reply(USER,run["id"],"later","later-key")
        store.claim(USER,run["id"]);store.finish(USER,run["id"],"completed","later answer")
        replay = client.post(f"/runs/{run['id']}/reply",json=body).json()
        assert replay["replayed"] and replay["answer"] == "reply answer"
        stream = client.get(f"/runs/{run['id']}/events?turn_key=reply-key").text
        assert 'reply answer' in stream and 'later answer' not in stream
        assert original["event_end"] < store.get_run(USER,run["id"])["event_end"]
    store.close()


@pytest.mark.services
@pytest.mark.asyncio
async def test_two_real_service_workers_execute_one_turn_once(postgres_store,cache):
    store = postgres_store
    kb = HybridKnowledge(store)
    model = client_with(response(text="处理完成"))
    one = DeskAgent(store,kb,model,coordination=cache)
    two = DeskAgent(store,kb,model,coordination=cache)
    run,_ = store.create_run(USER,"hello","multiworker-key")
    try:
        await asyncio.gather(one.execute(USER,run["id"]),two.execute(USER,run["id"]))
        assert model.messages.create.await_count == 1
        assert store.get_run(USER,run["id"])["answer"] == "处理完成"
        assert sum(m["role"]=="assistant" for m in store.history(USER,run["id"])) == 1
    finally:
        await one.close();await two.close();await cache.close()
