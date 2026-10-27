"""Current-entry trace contracts; offline models, actual execution and SQL pages."""
import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from resolvedesk.agent import DeskAgent
from resolvedesk.app import create_app
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.services import DeskServices
from resolvedesk.store import Store,Conflict
from resolvedesk.trace_queries import trace_page,recent_traces
from resolvedesk.tracing import summary
from test_resolvedesk import USER,OTHER,SUPPORT,FOREIGN,client_with,response
from test_it_execution import expert_role
from test_database_backends import postgres_store


@pytest.fixture
def store(tmp_path):
    result=Store(tmp_path/'traces.sqlite3')
    yield result
    result.close()


@pytest.mark.asyncio
async def test_main_inputs_outputs_errors_and_terminal_tool_are_persisted_redacted(store):
    model=client_with(response(tool='get_ticket',args={'ticket_id':'RD-FFFFFFFFFFFF'}),
        response(tool='ask_clarification',args={'question':'请补充范围，password=private-pass token=private-token'}))
    kb=HybridKnowledge(store);agent=DeskAgent(store,kb,model)
    run,_=store.create_run(USER,'账号问题 password=from-message','native-trace-key')
    try:
        await agent.execute(USER,run['id'])
        page=trace_page(store,USER,run['id'])['trace']
        calls=page['tool_calls'];assert [c['tool_name'] for c in calls]==['knowledge_search','get_ticket','ask_clarification']
        assert calls[1]['input']=={'ticket_id':'RD-FFFFFFFFFFFF'}
        assert calls[1]['error_code']=='KeyError' and not calls[1]['success']
        assert calls[2]['success'] and calls[2]['output_summary']['status']=='awaiting_input'
        assert all(type(c['latency_ms'])in {int,float} and c['latency_ms']>=0 for c in calls)
        with store.db() as db:raw=''.join(r[0] for r in db.execute('SELECT payload FROM events WHERE run_id=?',(run['id'],)))
        assert 'private-pass' not in raw and 'private-token' not in raw and 'from-message' not in raw
        assert page['latency_ms']>0 and page['timing_epoch']==1
        assert store.get_turn(USER,run['id'],'native-trace-key')['event_end']>=calls[-1]['event_seq']
    finally:await agent.close()


@pytest.mark.asyncio
async def test_specialist_real_mcp_calls_cache_and_fallback_have_one_trace_per_actual_call(store):
    kb=HybridKnowledge(store)
    kb.add(SUPPORT,'vpn','VPN','# VPN\n\nVPN连接超时先检查普通网络。')
    async def create(**kw):
        system=kw.get('system','')
        if system.startswith('你是企业IT意图分析器'):return response('{"fine_intents":["vpn_failure"],"confidence":0.96,"urgency":1}')
        if 'tools' not in kw:return response('[]')
        if system.startswith('你是ResolveDesk只读IT专业节点'):
            role=expert_role(kw)
            count=sum(isinstance(m.get('content'),list) for m in kw['messages'])
            if role=='technical' and count>=2:raise RuntimeError('synthetic failure after real read-only tool')
            if count<4:return response(tool='mcp_support_support_checklist',args={'category':'network'})
            return response('只读建议。')
        return response('请补充故障范围。')
    model=SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=create)),close=AsyncMock())
    services=DeskServices(store,kb,model);agent=DeskAgent(store,kb,model,services)
    run,_=store.create_run(USER,'VPN连接超时','specialist-trace-key')
    try:
        await services.start();await agent.execute(USER,run['id'])
        trace=trace_page(store,USER,run['id'])['trace']
        calls=[c for c in trace['tool_calls'] if c['layer']=='specialist']
        assert len(calls)==3
        assert [c['agent_type'] for c in calls]==['technical','general','general']
        assert all(c['source']=='external_mcp' and c['mcp_server']=='support' for c in calls)
        assert all(c['mcp_tool']=='support_checklist' and c['input']=={'category':'network'} for c in calls)
        assert all(c['output_summary'] for c in calls)
        assert any(c['cached'] for c in calls)
        assert len({c['trace_id'] for c in calls})==3
        assert trace['fallback_events'][0]['from_agent']=='technical'
        assert store.get_run(USER,run['id'])['error']=='specialist_partial'
    finally:await agent.close()


@pytest.mark.asyncio
async def test_cancelled_main_tool_records_failure_without_stealing_next_turn(store):
    model=client_with(response(tool='knowledge_search',args={'query':'VPN'}))
    kb=HybridKnowledge(store);agent=DeskAgent(store,kb,model)
    original=kb.search;gate=asyncio.Event()
    def search(*args,**kwargs):return original(*args,**kwargs)
    run,_=store.create_run(USER,'VPN','cancel-trace-key')
    # Bootstrap is real; gate only the subsequent requested tool.
    original_validate=agent._validate_sources
    async def validate(*args):await original_validate(*args)
    from unittest.mock import patch
    original_thread=asyncio.to_thread
    async def thread(func,*args,**kwargs):
        if func==kb.search and gate.is_set():await asyncio.Event().wait()
        result=await original_thread(func,*args,**kwargs)
        if func==kb.search:gate.set()
        return result
    try:
        with patch('asyncio.to_thread',side_effect=thread):
            agent.schedule(USER,run['id']);await asyncio.wait_for(gate.wait(),3)
            while model.messages.create.await_count==0:await asyncio.sleep(.001)
            await asyncio.sleep(.02);await agent.pause(USER,run['id'])
        calls=trace_page(store,USER,run['id'])['trace']['tool_calls']
        assert calls[-1]['error_code']=='CancelledError' and not calls[-1]['success']
        assert store.get_run(USER,run['id'])['status']=='paused'
    finally:await agent.close()


@pytest.mark.parametrize('backend',['sqlite','postgres'])
def test_trace_pages_are_complete_scoped_and_pinned_to_original_turn(store,request,backend):
    if backend=='postgres':store=request.getfixturevalue('postgres_store')
    run,_=store.create_run(USER,'first','page-first-key');token=store.claim_execution(USER,run['id'],'trace-owner')
    with store.execution(token):
        for i in range(255):store.event(USER,run['id'],'synthetic',{'n':i,'token':'must-redact'})
        store.finish(USER,run['id'],'completed','first answer')
    first=trace_page(store,USER,run['id'],limit=40)['trace']
    store.reply(USER,run['id'],'second','page-second-key');store.claim(USER,run['id']);store.finish(USER,run['id'],'completed','second answer')
    seen=list(first['events']);cursor=first['next_after']
    while cursor:
        page=trace_page(store,USER,run['id'],first['turn_key'],after=cursor,limit=40,through=first['snapshot_end'])['trace']
        seen+=page['events'];cursor=page['next_after']
    assert sum(e['kind']=='synthetic' for e in seen)==255
    assert len({e['seq'] for e in seen})==len(seen)
    assert 'second answer' not in json.dumps(seen)
    assert 'must-redact' not in json.dumps(seen)
    for p in (OTHER,SUPPORT,FOREIGN):
        with pytest.raises(KeyError):trace_page(store,p,run['id'])
    recent=recent_traces(store,USER,limit=1)
    assert recent['items'][0]['turn_key']=='page-second-key'
    assert recent_traces(store,USER,before=recent['next_before'],limit=1)['items'][0]['turn_key']=='page-first-key'
    assert not recent_traces(store,OTHER)['items']


def test_fenced_trace_cannot_be_written_by_old_epoch_and_interrupted_time_is_unknown(store):
    run,_=store.create_run(USER,'one','old-trace-key');old=store.claim_execution(USER,run['id'],'old-owner')
    store.recover();assert store.get_turn(USER,run['id'],'old-trace-key')['latency_ms'] is None
    store.resume(USER,run['id']);current=store.claim_execution(USER,run['id'],'new-owner')
    with store.execution(old),pytest.raises(Conflict):store.trace_tool(USER,run['id'],'old-trace-key',{'error_code':'CancelledError'})
    with store.execution(current):store.finish(USER,run['id'],'completed','new')
    assert trace_page(store,USER,run['id'])['trace']['timing_epoch']==2


def test_trace_http_validation_and_redaction_before_summary_truncation(store):
    kb=HybridKnowledge(store);agent=DeskAgent(store,kb,client_with())
    accounts={hashlib.sha256(b'trace-api-token').hexdigest():vars(USER)}
    secret='sk-'+('secret'*6)
    clean=summary({'very_long':'a'*1197+secret,'token':'hidden','url':'https://host/?key=private&x=1'})
    text=json.dumps(clean);assert secret not in text and 'hidden' not in text and 'key=private' not in text
    with TestClient(create_app(store,kb,agent,accounts)) as c:
        assert c.get('/trace/tools').status_code==401
        c.headers['Authorization']='Bearer trace-api-token'
        assert c.get('/trace/tools?limit=0').status_code==422
        assert c.get('/trace/tools?before=-1').status_code==422
        run,_=store.create_run(USER,'q','trace-validation-key')
        assert c.get('/trace/tool/'+run['id']+'?limit=501').status_code==422
        assert c.get('/trace/tool/'+run['id']+'?after=-1').status_code==422
