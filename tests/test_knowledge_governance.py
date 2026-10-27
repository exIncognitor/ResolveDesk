"""Publication authority across SQL, caches, vectors, APIs and frozen sandboxes."""
import asyncio
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from resolvedesk.app import create_app
from resolvedesk.eval_models import EvalDataset
from resolvedesk.eval_sandbox import EvalSandbox,freeze
from resolvedesk.eval_state import EvalState
from resolvedesk.knowledge_governance import KnowledgeMetadata
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.reliable_retrieval import ReliableRetrieval
from resolvedesk.store import Store,Principal,Conflict
from tooling.tool_manager import MCPToolManager
from test_resolvedesk import USER,OTHER,SUPPORT,FOREIGN,FakeEmbedding,client_with
from test_database_backends import postgres_store
from test_chroma_projection import vectors
from test_redis_coordination import cache


def metadata(**changes):
    return dict(material_type='technical_reference',source_urls=['https://learn.microsoft.com/example'],
        applicability=['Windows 11'],domains=['network_endpoint'],source_checked_on='2026-10-27',
        effective_from=1,expires_at=4102444700,**{})|changes


@pytest.fixture
def knowledge(tmp_path):
    store=Store(tmp_path/'publications.sqlite3');kb=HybridKnowledge(store,FakeEmbedding())
    try:yield kb
    finally:store.close()


def add(kb,p=SUPPORT,doc='policy',**kwargs):
    return kb.add(p,doc,'VPN Windows 11','# VPN\n\nVPN 连接超时排查，不修改真实企业制度。',metadata=metadata(),**kwargs)


def change(kb,action='publish',p=SUPPORT,doc='policy',**kwargs):
    row=next(r for r in kb.publications(p) if r['document_id']==doc)
    return kb.publication_action(p,doc,action,row['version'],row['revision'],'Synthetic source review',**kwargs)


def test_draft_publish_withdraw_source_spans_and_legacy_compatibility(knowledge):
    kb=knowledge;add(kb)
    assert not kb.search(USER,'VPN')['items'] and kb.stats(USER)['total_chunks']==0
    first=kb.scope_version(USER);published=change(kb)
    response=kb.search(USER,'VPN');item=response['items'][0]
    assert response['evidence_sufficient'] and item['publication']['metadata']['source_urls']
    assert item['content']=='# VPN\n\nVPN 连接超时排查，不修改真实企业制度。'[item['start']:item['end']]
    assert kb.citation(USER,item['citation'])['publication']['revision']==published['revision']
    assert kb.scope_version(USER)!=first
    change(kb,'withdraw')
    assert not kb.search(USER,'VPN')['items'] and not kb.validate_citations(USER,[item['citation']])
    with pytest.raises(KeyError):kb.citation(USER,item['citation'])
    assert len(kb.governance.audit_list(SUPPORT,'policy'))==3
    kb.add(SUPPORT,'legacy','VPN legacy','# VPN\nVPN legacy')
    assert 'publication' not in kb.search(USER,'VPN')['items'][0]


def test_content_and_metadata_updates_return_to_draft_without_omission_bypass(knowledge):
    kb=knowledge;add(kb);old=change(kb)
    change(kb,'metadata',metadata=metadata(applicability=['Windows 10']))
    assert kb.publications(SUPPORT)[0]['state']=='draft'
    with pytest.raises(Conflict):kb.publication_action(SUPPORT,'policy','publish',old['version'],old['revision'],'stale review')
    change(kb)
    kb.add(SUPPORT,'policy','VPN updated','# VPN\nVPN changed content')
    row=kb.publications(SUPPORT)[0]
    assert row['state']=='draft' and row['metadata']['applicability']==['Windows 10']
    assert not kb.search(USER,'VPN')['items']


def test_sql_time_expiry_invalidates_warm_lexical_cache_without_write(knowledge):
    kb=knowledge
    kb.add(SUPPORT,'policy','VPN','# VPN\nVPN',metadata=metadata(expires_at=time.time()+.5))
    change(kb);other=HybridKnowledge(kb.store,FakeEmbedding())
    item=other.search(USER,'VPN')['items'][0];version=other.scope_version(USER)
    time.sleep(.6)
    assert other.scope_version(USER)!=version and not other.search(USER,'VPN')['items']
    assert other.stats(USER)['total_chunks']==0
    with pytest.raises(KeyError):other.citation(USER,item['citation'])
    with pytest.raises(Conflict):change(kb)


def test_future_publication_and_private_tenant_review_boundaries(knowledge):
    kb=knowledge;add(kb,visibility='private');row=change(kb)
    assert not kb.search(USER,'VPN')['items']
    assert kb.search(SUPPORT,'VPN')['items']
    assert kb.publications(Principal('demo','bob','support'))==[] and kb.publications(FOREIGN)==[]
    for p in (USER,OTHER):
        with pytest.raises(PermissionError):kb.publication_action(p,'policy','withdraw',row['version'],row['revision'],'not authorized')
    with pytest.raises(KeyError):kb.publication_action(Principal('demo','bob','support'),'policy','withdraw',row['version'],row['revision'],'private')
    kb.add(SUPPORT,'future','VPN future','# VPN\nVPN',metadata=metadata(effective_from=time.time()+.5))
    change(kb,doc='future');assert not any(i['document_id']=='future' for i in kb.search(USER,'VPN')['items'])
    time.sleep(.6)
    assert any(i['document_id']=='future' for i in kb.search(USER,'VPN')['items'])


@pytest.mark.parametrize('changes',[
    {'source_urls':['http://example.com']},{'source_urls':['https://u:secret@example.com']},
    {'source_urls':[]},{'applicability':['']},{'expires_at':1},{'source_checked_on':'2026-02-31'},
    {'effective_from':float('nan')},{'source_urls':['https://example.com/\nheader']},
])
def test_metadata_validation(changes):
    with pytest.raises(ValidationError):KnowledgeMetadata.model_validate(metadata(**changes))


def test_review_api_enforces_authenticated_role_and_both_versions(knowledge):
    from resolvedesk.agent import DeskAgent
    kb=knowledge;add(kb)
    app=create_app(kb.store,kb,DeskAgent(kb.store,kb,client_with()),accounts={hashlib.sha256(k.encode()).hexdigest():{'tenant':'demo','user':user,'role':role} for k,user,role in [('support-token','support','support'),('user-token','alice','user')]})
    with TestClient(app) as api:
        assert api.get('/knowledge/publications').status_code==401
        assert api.get('/knowledge/publications',headers={'Authorization':'Bearer user-token'}).status_code==403
        headers={'Authorization':'Bearer support-token'}
        row=api.get('/knowledge/publications',headers=headers).json()[0]
        body={'action':'publish','version':row['version'],'revision':row['revision'],'note':'source review'}
        assert api.post('/knowledge/publications/policy/review',headers=headers,json=body).status_code==200
        assert api.post('/knowledge/publications/policy/review',headers=headers,json=body).status_code==409
        assert api.get('/knowledge/stats',headers={'Authorization':'Bearer user-token'}).json()['total_chunks']>0
        assert api.get('/knowledge/publications/policy/audit',headers=headers).json()[-1]['action']=='publish'


@pytest.mark.asyncio
async def test_legacy_paused_checkpoint_refreshes_after_metadata_enrollment(knowledge):
    from unittest.mock import AsyncMock
    from resolvedesk.agent import DeskAgent
    kb=knowledge
    kb.add(SUPPORT,'policy','VPN','# VPN\nVPN legacy')
    source=kb.search(USER,'VPN')['items'][0]
    run,_=kb.store.create_run(USER,'VPN 连接超时','legacy-publication-checkpoint')
    kb.store.claim(USER,run['id'])
    kb.store.checkpoint(USER,run['id'],{'phase':'loop','sources':{source['citation']:source}})
    kb.add(SUPPORT,'policy','VPN','# VPN\nVPN legacy',metadata=metadata());change(kb)
    assert kb.citation(USER,source['citation'])['version']==source['version']
    agent=DeskAgent(kb.store,kb,client_with())
    agent._bootstrap=AsyncMock(return_value=None)
    try:
        with pytest.raises(RuntimeError):await agent._validate_sources(USER,{source['citation']:source})
        await agent._loop(USER,run['id'])
        assert agent._bootstrap.await_count==1 and agent._bootstrap.call_args.args[-1] is None
    finally:await agent.close()


@pytest.mark.asyncio
async def test_frozen_sandbox_preserves_publication_authority(tmp_path,knowledge):
    kb=knowledge;add(kb);change(kb);add(kb,doc='draft')
    state=EvalState(kb.store,'synthetic-governance-snapshot')
    manifest=freeze(state,SUPPORT,EvalDataset(intent_cases=[{'message':'你好','expected':'general'}]).model_dump(),Path(__file__).resolve().parents[1])
    assert {r['document_id'] for r in manifest['evidence']}=={'policy'}
    assert len(manifest['publications'])==1
    sandbox=EvalSandbox(kb.store,kb,manifest,tmp_path/'sandbox')
    try:
        await sandbox.open(SUPPORT)
        assert sandbox.kb.search(USER,'VPN')['items'][0]['publication']['metadata']==metadata()
        change(sandbox.kb,'withdraw')
        assert not sandbox.kb.search(USER,'VPN')['items']
    finally:await sandbox.close()


@pytest.mark.asyncio
async def test_insufficient_managed_evidence_stops_before_specialist_or_policy_generation(knowledge):
    from resolvedesk.agent import DeskAgent
    from resolvedesk.services import DeskServices
    from test_it_execution import model_for
    kb=knowledge;add(kb);change(kb)
    model=model_for();services=DeskServices(kb.store,kb,model,mcp_configs=[])
    # Explicit false confidence from the real search layer is not an empty
    # candidate list: the coordinator must not turn ranked material into facts.
    actual=services.search
    async def insufficient(*args,**kwargs):return await actual(*args,**kwargs)|{'evidence_sufficient':False}
    services.search=insufficient
    run,_=kb.store.create_run(USER,'设备的VPN为什么不能连接','insufficient-managed')
    agent=DeskAgent(kb.store,kb,model,services)
    try:
        await agent.execute(USER,run['id'])
        result=kb.store.get_run(USER,run['id'])
        assert result['status']=='awaiting_input' and result['error']=='insufficient_evidence'
        assert not any(kwargs.kwargs.get('system','').startswith('你是 ResolveDesk') for kwargs in model.messages.create.call_args_list)
    finally:await agent.close()


@pytest.mark.services
@pytest.mark.asyncio
async def test_real_pg_redis_chroma_publication_cache_and_failed_projection(postgres_store,vectors,cache,monkeypatch):
    first=HybridKnowledge(postgres_store,FakeEmbedding(),vectors)
    second=HybridKnowledge(postgres_store,FakeEmbedding(),vectors)
    model=client_with();one=ReliableRetrieval(first,model,MCPToolManager(api_key='unused'),cache)
    two=ReliableRetrieval(second,model,MCPToolManager(api_key='unused'),cache)
    try:
        add(first);assert vectors.count()==0
        change(first)
        assert vectors.count()>0
        result=await one.search(USER,'VPN',optimize=False)
        assert (await two.search(USER,'VPN',optimize=False))['cached']
        original=vectors.replace
        monkeypatch.setattr(vectors,'replace',lambda *a:(_ for _ in ()).throw(ConnectionError('synthetic')))
        change(first,'withdraw')
        assert vectors.count()>0  # Stale remote bytes remain; SQL still denies.
        assert not (await two.search(USER,'VPN',optimize=False))['items']
        assert second.stats(USER)['total_chunks']==0
        with pytest.raises(KeyError):second.citation(USER,result['items'][0]['citation'])
        monkeypatch.setattr(vectors,'replace',original)
        with postgres_store.db(True) as db:db.execute("UPDATE index_outbox SET next_attempt=0 WHERE state='pending'")
        assert first.drain_index()['pending']==0 and vectors.count()==0
        change(first)
        assert (await two.search(USER,'VPN',optimize=False))['items']
    finally:await cache.close()


@pytest.mark.services
def test_real_scheduled_projection_does_not_acknowledge_future_boundaries(postgres_store,vectors):
    kb=HybridKnowledge(postgres_store,FakeEmbedding(),vectors)
    kb.add(SUPPORT,'policy','VPN','# VPN\nVPN',metadata=metadata(effective_from=time.time()+.6,expires_at=time.time()+1.7))
    change(kb)
    assert vectors.count()==0 and kb.drain_index()['pending']==0
    with postgres_store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM index_outbox WHERE state='pending'").fetchone()[0]==2
    time.sleep(.7)
    kb.drain_index();assert vectors.count()>0 and not kb.search(USER,'VPN')['degraded']
    version=kb.scope_version(USER)
    time.sleep(1.1)
    assert kb.scope_version(USER)!=version and not kb.search(USER,'VPN')['items']
    kb.drain_index();assert vectors.count()==0
