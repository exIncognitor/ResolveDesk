import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from resolvedesk.agent import DeskAgent
from resolvedesk.evaluations import EvaluationService
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.services import DeskServices, ScopedMemory
from resolvedesk.store import Conflict, Principal, Store
from test_resolvedesk import FakeEmbedding, USER, OTHER, FOREIGN, SUPPORT, FIELDS, response, client_with


@pytest.fixture
def store(tmp_path):return Store(tmp_path/'integrated.sqlite3')


@pytest.mark.asyncio
async def test_checkpoint_pending_model_result_is_not_repeated(store):
    kb=HybridKnowledge(store)
    run,_=store.create_run(USER,'创建工单','pending-model-result')
    store.claim(USER,run['id'])
    store.checkpoint(USER,run['id'],{'messages':[{'role':'user','content':'创建工单'}],'system':'test','sources':{},'role':'general','step':0,
        'pending':{'blocks':response(tool='prepare_ticket',args=FIELDS).content,'stop_reason':'tool_use'}})
    store.recover();store.resume(USER,run['id'])
    model=client_with()
    await DeskAgent(store,kb,model).execute(USER,run['id'])
    assert store.get_run(USER,run['id'])['status']=='awaiting_confirmation'
    model.messages.create.assert_not_called()
    assert not store.list_tickets(USER)


@pytest.mark.asyncio
async def test_checkpoint_after_tool_preserves_results_and_reply_invalidates(store):
    run,_=store.create_run(USER,'查询工单','after-tool-result')
    store.claim(USER,run['id'])
    messages=[{'role':'user','content':'查询工单'},{'role':'assistant','content':[{'type':'tool_use','id':'a','name':'get_ticket','input':{'ticket_id':'RD-123456789ABC'}}]},
              {'role':'user','content':[{'type':'tool_result','tool_use_id':'a','content':'{"status":"open"}'}]}]
    store.checkpoint(USER,run['id'],{'messages':messages,'sources':{},'system':'test','role':'general','step':1,'pending':None})
    store.recover();store.resume(USER,run['id'])
    model=client_with(response('工单状态为 open'))
    await DeskAgent(store,HybridKnowledge(store),model).execute(USER,run['id'])
    assert model.messages.create.call_count==1
    assert model.messages.create.call_args.kwargs['messages']==messages
    store.reply(USER,run['id'],'修改问题')
    assert store.load_checkpoint(USER,run['id']) is None
    with pytest.raises(KeyError):store.load_checkpoint(OTHER,run['id'])


@pytest.mark.asyncio
async def test_memory_summary_profile_semantic_recall_and_isolation(store):
    model=client_with(response('用户使用 Windows 11 和 IntelliJ，VPN 故障仍未解决。'))
    memory=ScopedMemory(store,FakeEmbedding(),model)
    old,_=store.create_run(USER,'我使用 Windows 11 和 IntelliJ，VPN 连接失败','memory-old')
    store.claim(USER,old['id']);store.finish(USER,old['id'],'completed','VPN 检查网络')
    await memory.remember(USER,old['id'])
    current,_=store.create_run(USER,'VPN 又出问题了','memory-new')
    for i in range(6):
        store.claim(USER,current['id']);store.finish(USER,current['id'],'awaiting_input',f'请补充 {i}')
        store.reply(USER,current['id'],f'VPN 补充信息 {i}')
    context=await memory.context(USER,current['id'])
    assert context['summary'] and context['profile']['os']=='Windows 11'
    assert context['relevant_history'][0]['run_id']==old['id']
    assert memory.inspect(OTHER)=={'profile':{},'episodes':0,'summaries':0}
    with pytest.raises(KeyError):await memory.context(FOREIGN,current['id'])
    await memory.context(USER,current['id'])
    assert model.messages.create.call_count==1


@pytest.mark.asyncio
async def test_real_mcp_subprocess_is_integrated_and_read_only(store):
    svc=DeskServices(store,HybridKnowledge(store,FakeEmbedding()),client_with())
    try:
        await svc.start()
        assert svc.mcp.summary()['servers'][0]['state']=='connected'
        tools=svc.external_tools('technical')
        assert set(tools)=={'mcp_support_support_checklist'}
        result=await svc.call_external(USER,'mcp_support_support_checklist',{'category':'network'},'technical')
        assert result['structured']['category']=='network'
        assert not store.list_tickets(USER)
        with pytest.raises(PermissionError):await svc.call_external(USER,'create_ticket',{},'technical')
    finally:await svc.close()


@pytest.mark.asyncio
async def test_rewrite_fusion_rerank_and_failure_fallback(store):
    kb=HybridKnowledge(store,FakeEmbedding())
    for i in range(8):kb.add(SUPPORT,f'vpn{i}',f'VPN {i}',f'VPN 连接超时 排查网络 {i}')
    model=client_with(response('["VPN 连接超时", "VPN 网络排查"]'),response('[5, 0, 0]'))
    svc=DeskServices(store,kb,model,mcp_configs=[])
    try:
        result=await svc.search(USER,'VPN',3)
        assert len(result['queries'])==3 and result['reranked'] and len(result['items'])==3
        assert len({i['citation'] for i in result['items']})==3
        model.messages.create.side_effect=TimeoutError()
        fallback=await svc.search(USER,'VPN',3)
        assert fallback['items'] and not fallback['reranked']
        assert len(fallback['errors'])==2
        assert svc.skills.summary()['count']==3
        assert all('history' not in s['path'].split('\\') for s in svc.skills.summary()['skills'])
    finally:await svc.close()


@pytest.mark.asyncio
async def test_original_routing_and_parallel_specialists_are_used(store):
    from core.intent_recognizer import IntentCategory,UrgencyLevel
    svc=DeskServices(store,HybridKnowledge(store),client_with(),mcp_configs=[])
    svc.roles._intent_recognizer.recognize=AsyncMock(return_value=SimpleNamespace(intent=IntentCategory.TECHNICAL_LOGIN,intent_group='technical',urgency=UrgencyLevel.MEDIUM,confidence=.9,entities={}))
    svc.roles._execute=AsyncMock(return_value=SimpleNamespace(success=True,content='专业只读建议'))
    try:
        packet=await svc.route(USER,'run','登录报错 401，同时订阅被重复扣款',[],{})
        assert packet['primary']=='technical' and 'billing' in packet['supporting']
        assert svc.roles._execute.call_count==2
        assert len(packet['specialist_opinions'])==2
        assert svc.skills.prompt_for('登录报错 401','technical')
    finally:await svc.close()


@pytest.mark.asyncio
async def test_reviewed_feedback_runs_as_persistent_scoped_regression(store):
    kb=HybridKnowledge(store,FakeEmbedding())
    kb.add(SUPPORT,'vpn','VPN','# VPN\n\n网络排查')
    run,_=store.create_run(USER,'VPN 网络排查','feedback-source')
    store.feedback(USER,run['id'],'bad','召回不准')
    service=EvaluationService(store,kb)
    with pytest.raises(PermissionError):service.promote(USER,run['id'],'vpn','VPN')
    with pytest.raises(KeyError):service.promote(FOREIGN,run['id'],'vpn','VPN')
    with pytest.raises(ValueError):service.promote(SUPPORT,run['id'],'unknown','VPN')
    service.promote(SUPPORT,run['id'],'vpn','VPN')
    job=service.submit(SUPPORT)
    await asyncio.gather(*list(service.tasks.values()))
    report=service.get(SUPPORT,job['job_id'])
    assert report['status']=='completed' and report['result']['total']==21
    assert report['result']['results'][-1]['passed']
    with pytest.raises(KeyError):service.get(USER,job['job_id'])
    restarted=EvaluationService(Store(store.path),kb)
    assert restarted.get(SUPPORT,job['job_id'])['status']=='completed'
    await service.close();await restarted.close()


def test_integrated_http_surfaces_enforce_roles_and_persist_feedback_cases(store):
    import hashlib
    import time
    from fastapi.testclient import TestClient
    from resolvedesk.app import create_app
    kb=HybridKnowledge(store,FakeEmbedding())
    kb.add(SUPPORT,'vpn','VPN','# VPN\n\n网络排查')
    model=client_with()
    svc=DeskServices(store,kb,model,mcp_configs=[])
    agent=DeskAgent(store,kb,model,svc)
    tokens={'alice':USER,'support':SUPPORT,'bob':OTHER}
    accounts={hashlib.sha256(t.encode()).hexdigest():vars(p) for t,p in tokens.items()}
    uh={'Authorization':'Bearer alice'};sh={'Authorization':'Bearer support'};bh={'Authorization':'Bearer bob'}
    run,_=store.create_run(USER,'VPN 网络排查','http-surfaces')
    store.claim(USER,run['id']);store.event(USER,run['id'],'routing',{'primary':'technical'});store.finish(USER,run['id'],'completed','请检查网络')
    with TestClient(create_app(store,kb,agent,accounts)) as c:
        assert c.get('/skills',headers=uh).json()['count']==3
        assert c.post('/skills/reload',headers=uh).status_code==403
        assert c.post('/skills/reload',headers=sh).json()['count']==3
        assert c.get('/mcp',headers=uh).json()=={'servers':[]}
        assert c.get('/memory',headers=uh).json()['episodes']==0
        assert c.get('/monitor',headers=uh).json()['agent_stats']['technical']['total']==1
        assert c.get('/monitor',headers=bh).json()['agent_stats']=={}
        assert c.get('/runs/'+run['id']+'/messages',headers=bh).status_code==404
        assert c.get('/history',headers=uh).status_code==404
        assert c.get('/assets/history',headers=uh).status_code==404
        assert c.post('/runs/'+run['id']+'/feedback',headers=uh,json={'rating':'bad','note':'需要回归'}).status_code==200
        assert c.post('/evaluation/candidates/'+run['id']+'/promote',headers=uh,json={'document':'vpn','section':'VPN'}).status_code==403
        assert c.post('/evaluation/candidates/'+run['id']+'/promote',headers=sh,json={'document':'vpn','section':'VPN'}).status_code==200
        assert c.post('/eval/run',headers=uh).status_code==403
        job=c.post('/eval/run',headers=sh).json()
        assert c.get('/eval/jobs/'+job['job_id'],headers=uh).status_code==404
        for _ in range(100):
            result=c.get('/eval/jobs/'+job['job_id'],headers=sh).json()
            if result['status']=='completed':break
            time.sleep(.01)
        assert result['result']['total']==9
        model.messages.create.assert_not_called()
