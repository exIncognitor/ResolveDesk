"""Actual Chroma service, SQL projection retries and ACL revocation."""
from concurrent.futures import ThreadPoolExecutor
import os
import uuid

import pytest

from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.settings import ServiceSettings
from resolvedesk.store import Principal
from resolvedesk.vectors import ChromaVectors
from test_database_backends import postgres_store
from test_resolvedesk import FakeEmbedding,USER,OTHER,SUPPORT,FOREIGN


@pytest.fixture
def vectors():
    if os.getenv("RESOLVEDESK_SERVICE_TESTS")!="true": pytest.skip("Real service tests disabled")
    name = "test-rd-"+uuid.uuid4().hex
    vectors = ChromaVectors(ServiceSettings.load().chroma_url,FakeEmbedding.identity,collection=name)
    try: yield vectors
    finally:
        # Only this fixture's unique synthetic collection may be removed.
        assert vectors.name==name and name.startswith("test-rd-")
        vectors.request("DELETE",vectors.base+"/"+name)
        vectors.close()


@pytest.mark.services
def test_real_chroma_prefilter_and_sql_revalidation(postgres_store,vectors):
    kb = HybridKnowledge(postgres_store,FakeEmbedding(),vectors)
    kb.add(SUPPORT,"public","VPN public","# VPN\n\nVPN 连接检查")
    kb.add(SUPPORT,"private","VPN private","# VPN\n\nVPN 内部账号",visibility="private")
    kb.add(FOREIGN,"foreign","VPN foreign","# VPN\n\nVPN 他租户")
    assert {r["metadata"]["document_id"] for r in vectors.search(USER,[1.,0.])}=={"public"}
    result=kb.search(USER,"VPN",mode="vector")
    assert not result["degraded"] and {r["document_id"] for r in result["items"]}=={"public"}
    citation=result["items"][0]["citation"]
    kb.add(SUPPORT,"public","VPN revised","# VPN\n\nVPN 私有新版",visibility="private")
    assert not kb.search(USER,"VPN",mode="vector")["items"]
    with pytest.raises(KeyError): kb.citation(USER,citation)
    assert {r["document_id"] for r in kb.search(SUPPORT,"VPN",mode="vector")["items"]}=={"public","private"}


@pytest.mark.services
def test_index_outage_keeps_sql_and_retry_projects_latest_version(postgres_store,vectors,monkeypatch):
    kb = HybridKnowledge(postgres_store,FakeEmbedding(),vectors)
    kb.add(SUPPORT,"policy","VPN policy","# VPN\n\nVPN old")
    original=vectors.replace
    def unavailable(*args): raise ConnectionError("synthetic outage")
    monkeypatch.setattr(vectors,"replace",unavailable)
    changed=kb.add(SUPPORT,"policy","VPN new","# VPN\n\nVPN new")
    assert changed["index_pending"]
    result=kb.search(USER,"VPN",mode="hybrid")
    assert result["degraded"] and result["items"][0]["version"]==changed["version"]
    assert not kb.search(USER,"VPN",mode="vector")["items"]  # Stale index cannot supply old citations.
    kb.add(SUPPORT,"policy","VPN latest","# VPN\n\nVPN latest")
    monkeypatch.setattr(vectors,"replace",original)
    with postgres_store.db(True) as db: db.execute("UPDATE index_outbox SET next_attempt=0 WHERE state='pending'")
    assert kb.drain_index()["pending"]==0
    assert kb.search(USER,"VPN",mode="vector")["items"][0]["title"]=="VPN latest"
    assert vectors.count()==1


@pytest.mark.services
def test_competing_index_workers_and_document_replacement(postgres_store,vectors):
    kb=HybridKnowledge(postgres_store,FakeEmbedding(),vectors)
    def update(index): return kb.add(SUPPORT,"shared","VPN "+str(index),"# VPN\n\nVPN update "+str(index))
    with ThreadPoolExecutor(max_workers=4) as pool: list(pool.map(update,range(8)))
    kb.drain_index()
    latest=kb.search(USER,"VPN",mode="vector")
    assert not latest["degraded"] and len(latest["items"])==1
    assert vectors.count()==1
