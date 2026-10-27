"""G06 actual service/API cache, fault, circuit and output authorization contracts."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from resolvedesk.agent import DeskAgent
from resolvedesk.app import create_app
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.services import DeskServices
from resolvedesk.store import Store
from tooling.tool_manager import CircuitState,Tool,MCPToolManager
from test_resolvedesk import USER,OTHER,FOREIGN,SUPPORT,FakeEmbedding,response,client_with
from test_database_backends import postgres_store
from test_chroma_projection import vectors
from test_redis_coordination import cache


@pytest.fixture
def environment(tmp_path):
    store=Store(tmp_path/'reliable.sqlite3');kb=HybridKnowledge(store,FakeEmbedding())
    for index in range(8):kb.add(SUPPORT,'vpn'+str(index),'VPN '+str(index),'# VPN\n\nVPN 连接超时 网络排查 '+str(index))
    async def create(**kwargs):
        return response('["VPN 网络故障"]' if '检索查询' in kwargs.get('system','') else '[0,1]')
    model=SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=create)),close=AsyncMock())
    services=DeskServices(store,kb,model,mcp_configs=[])
    yield store,kb,model,services
    store.close()


@pytest.mark.asyncio
async def test_full_pipeline_cache_precedes_paid_stages_and_returns_copies(environment):
    store,kb,model,services=environment
    try:
        first=await services.search(USER,'VPN',2)
        assert first['reranked'] and not first['cached'] and model.messages.create.call_count==2
        first['items'][0]['content']='caller mutation'
        second=await services.search(USER,'VPN',2)
        assert second['cached'] and model.messages.create.call_count==2
        assert all(r['content']!='caller mutation' for r in second['items'])
        await services.search(USER,'VPN',2,role='technical')
        assert model.messages.create.call_count==4
        await services.search(OTHER,'VPN',2)
        assert model.messages.create.call_count==6
    finally:await services.close()


@pytest.mark.asyncio
async def test_malformed_cache_is_ignored_and_unknown_model_fields_never_enter_sources(environment):
    store,kb,model,services=environment
    class BrokenCache:
        async def get_json(self,scope):return {'items':[]}
        async def set_json(self,*args,**kwargs):pass
    services.retrieval.coordination=BrokenCache()
    try:
        result=await services.search(USER,'VPN',optimize=False)
        assert result['items'] and not result['cached'] and not result['degraded']
    finally:await services.close()


@pytest.mark.asyncio
async def test_api_free_modes_share_manager_and_native_cache_expiry(environment,monkeypatch):
    import resolvedesk.reliable_retrieval as pipeline
    import tooling.tool_manager as manager_module
    store,kb,model,services=environment
    token='offline-test-token';agent=DeskAgent(store,kb,model,services)
    app=create_app(store,kb,agent,{hashlib.sha256(token.encode()).hexdigest():vars(USER)})
    clock=[100.]
    monkeypatch.setattr(pipeline,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    monkeypatch.setattr(manager_module,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test',headers={'Authorization':'Bearer '+token}) as http:
            for mode in ('bm25','vector','hybrid'):
                first=await http.post('/knowledge/search',json={'query':'VPN','mode':mode});first.raise_for_status()
                assert first.json()['mode']==mode and not first.json()['cached']
                second=await http.post('/knowledge/search',json={'query':'VPN','mode':mode})
                assert second.json()['cached']
            total=services.manager._tools['it_rag'].stats.total
            clock[0]+=61
            fresh=await http.post('/knowledge/search',json={'query':'VPN','mode':'hybrid'})
            assert not fresh.json()['cached'] and services.manager._tools['it_rag'].stats.total==total+1
        model.messages.create.assert_not_called()
    finally:await agent.close()


@pytest.mark.asyncio
async def test_semantic_failures_open_circuit_one_half_open_probe_then_recover(environment,monkeypatch):
    store,kb,model,services=environment
    original=kb.search;calls=[]
    def broken(p,q,k,mode):
        calls.append(mode)
        if mode!='bm25':raise ConnectionError('synthetic semantic outage')
        return original(p,q,k,mode)
    monkeypatch.setattr(kb,'search',broken)
    tool=services.manager._tools['it_rag']
    try:
        for index in range(5):
            result=await services.search(USER,'VPN '+str(index),optimize=False)
            assert result['items'] and result['degraded'] and result['mode']=='hybrid' and result['effective_mode']=='bm25'
        assert tool.breaker.state==CircuitState.OPEN and calls.count('hybrid')==5
        await services.search(USER,'VPN rejected',optimize=False)
        assert calls.count('hybrid')==5
        tool.breaker.recovery_s=0
        entered,release=asyncio.Event(),asyncio.Event()
        original_handler=tool.handler
        async def probe(params,context):
            entered.set();await release.wait()
            return await original_handler(params,context)
        tool.handler=probe;monkeypatch.setattr(kb,'search',original)
        task=asyncio.create_task(services.search(USER,'VPN recovery',optimize=False))
        await entered.wait()
        blocked=await services.search(USER,'VPN concurrent',optimize=False)
        assert tool.breaker.state==CircuitState.HALF_OPEN and blocked['degraded']
        release.set();recovered=await task
        assert not recovered['degraded'] and tool.breaker.state==CircuitState.CLOSED
    finally:await services.close()


@pytest.mark.asyncio
async def test_explicit_vector_mode_never_silently_becomes_lexical(environment,monkeypatch):
    store,kb,model,services=environment
    original=kb.search
    def broken(p,q,k,mode):
        if mode=='vector':raise TimeoutError('synthetic')
        return original(p,q,k,mode)
    monkeypatch.setattr(kb,'search',broken)
    try:
        result=await services.search(USER,'VPN',optimize=False,mode='vector')
        assert result['items']==[] and result['degraded'] and result['mode']=='vector' and result['effective_mode']=='unavailable'
        assert services.manager._tools['it_bm25'].stats.total==0
    finally:await services.close()


@pytest.mark.asyncio
async def test_timeout_keeps_native_threads_bounded_and_lexical_fallback_responsive(environment,monkeypatch):
    store,kb,model,services=environment
    original=kb.search;release=threading.Event();started=[]
    def hung(p,q,k,mode):
        if mode=='hybrid':started.append(q);release.wait(3)
        return original(p,q,k,mode)
    monkeypatch.setattr(kb,'search',hung)
    tool=services.manager._tools['it_rag'];tool.timeout_s=.04;tool.breaker.threshold=2
    try:
        for index in range(3):
            async with asyncio.timeout(.5):result=await services.search(USER,'VPN '+str(index),optimize=False)
            assert result['items'] and result['effective_mode']=='bm25' and result['degraded']
        assert len(started)==2 and services.retrieval.native_slots._value==2
    finally:
        release.set()
        async with asyncio.timeout(4):
            while services.retrieval.native_slots._value!=4:await asyncio.sleep(.01)
        await services.close()


@pytest.mark.asyncio
async def test_rewrite_and_rerank_deadlines_keep_original_evidence(environment):
    store,kb,model,services=environment
    async def slow(**kwargs):await asyncio.sleep(2)
    model.messages.create.side_effect=slow
    services.retrieval.model_timeout=.02
    try:
        async with asyncio.timeout(.5):result=await services.search(USER,'VPN',2)
        assert result['items'] and not result['reranked'] and len(result['errors'])==2
        assert all('TimeoutError' in error for error in result['errors'])
    finally:await services.close()


@pytest.mark.asyncio
async def test_total_knowledge_outage_stops_policy_generation(environment,monkeypatch):
    from test_it_execution import model_for
    store,kb,model,old_services=environment
    await old_services.close()
    calls=[];model=model_for(calls=calls);services=DeskServices(store,kb,model,mcp_configs=[])
    monkeypatch.setattr(kb,'search',lambda *a:(_ for _ in ()).throw(ConnectionError('synthetic outage')))
    agent=DeskAgent(store,kb,model,services)
    run,_=store.create_run(USER,'VPN连不上，怎么办','no-knowledge-evidence')
    try:
        await agent.execute(USER,run['id'])
        final=store.get_run(USER,run['id'])
        assert final['status']=='awaiting_input' and final['error']=='knowledge_unavailable'
        assert '没有可核验' in final['answer'] and calls==['intent'] and not store.list_tickets(USER)
    finally:await agent.close()


@pytest.mark.asyncio
async def test_acl_or_content_change_during_generation_blocks_stale_final_and_new_stream_batches(environment):
    store,kb,model,services=environment
    source=kb.search(USER,'VPN',1)['items'][0]
    run,_=store.create_run(USER,'VPN排查','mid-stream-knowledge-change')
    store.claim(USER,run['id'])
    state={'messages':[{'role':'user','content':'VPN排查'}],'system':'test','sources':{source['citation']:source},'role':'general','step':0,'pending':None}
    store.checkpoint(USER,run['id'],state);store.recover();store.resume(USER,run['id'])
    class Stream:
        async def deltas(self):
            yield '当前授权时允许发送的第一批文字。'*5
            kb.add(SUPPORT,source['document_id'],'私有替换','新版私有资料',visibility='private')
            yield 'REVOKED_MUST_NOT_BE_SENT'*5
        @property
        def text_stream(self):return self.deltas()
        async def get_final_message(self):return response('旧引用 ['+source['citation']+']')
    @asynccontextmanager
    async def stream(**kwargs):yield Stream()
    model.messages.stream=stream
    agent=DeskAgent(store,kb,model,services)
    try:
        await agent.execute(USER,run['id'])
        final=store.get_run(USER,run['id'])
        deltas=[e['payload']['text'] for e in store.events(USER,run['id']) if e['kind']=='answer_delta']
        assert len(deltas)==1 and all('REVOKED_MUST_NOT_BE_SENT' not in d for d in deltas)
        assert final['status']=='failed' and source['citation'] not in final['answer']
    finally:await agent.close()


@pytest.mark.services
@pytest.mark.asyncio
async def test_real_cross_worker_redis_cache_reimport_acl_and_chroma_failure(postgres_store,vectors,cache,monkeypatch):
    kb=HybridKnowledge(postgres_store,FakeEmbedding(),vectors)
    kb.add(SUPPORT,'vpn','VPN','VPN 连接排查')
    first=DeskServices(postgres_store,kb,client_with(),mcp_configs=[],coordination=cache)
    second=DeskServices(postgres_store,kb,client_with(),mcp_configs=[],coordination=cache)
    try:
        result=await first.search(USER,'VPN',optimize=False)
        assert not result['degraded'] and not result['cached']
        copied=await second.search(USER,'VPN',optimize=False)
        assert copied['cached'] and copied['cache_layer']=='redis' and second.manager._tools['it_rag'].stats.total==0
        old=result['items'][0]['citation']
        kb.add(SUPPORT,'vpn','VPN','VPN 连接排查',visibility='private')
        revoked=await second.search(USER,'VPN',optimize=False)
        assert revoked['items']==[] and not revoked['cached']
        assert kb.validate_citations(USER,[old])=={} and kb.validate_citations(SUPPORT,[old])
        kb.add(SUPPORT,'vpn','VPN new','VPN 新版故障排查',visibility='tenant')
        updated=await second.search(USER,'VPN',optimize=False)
        assert not updated['cached'] and updated['items'][0]['citation']!=old
        assert (await first.search(FOREIGN,'VPN',optimize=False))['items']==[]
        monkeypatch.setattr(vectors,'search',lambda *a,**kw:(_ for _ in ()).throw(ConnectionError('synthetic Chroma outage')))
        degraded=await first.search(USER,'VPN 新故障',optimize=False)
        assert degraded['degraded'] and degraded['effective_mode']=='bm25' and degraded['items']
    finally:
        await first.close();await second.close()
        keys=[key async for key in cache.redis.scan_iter(match=cache.namespace+':*')]
        assert all(key.startswith(cache.namespace+':') for key in keys)
        if keys:await cache.redis.delete(*keys)
        await cache.close()


@pytest.mark.asyncio
async def test_sync_fallback_timeout_is_nonblocking_bounded_and_error_text_is_sanitized():
    release=threading.Event();started=[];manager=MCPToolManager(api_key='offline')
    async def broken(*args):raise RuntimeError('password=synthetic-do-not-log')
    def fallback(*args):started.append(True);release.wait(2);return 'late'
    tool=Tool('bounded','',broken,{},fallback=fallback,fallback_timeout_s=.03)
    manager.register(tool)
    try:
        for _ in range(4):
            async with asyncio.timeout(.2):result=await manager.call('bounded',{})
            assert not result.success and 'synthetic-do-not-log' not in result.error
        assert len(started)==2
    finally:
        release.set()
        async with asyncio.timeout(3):
            while tool.fallback_slots._value!=2:await asyncio.sleep(.01)
        await manager._client.close()
