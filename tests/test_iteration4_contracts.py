"""Independent contract regressions: controlled models, no paid API calls."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from resolvedesk.agent import DeskAgent
from resolvedesk.business import BusinessService
from resolvedesk.quality import suite_identity
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.services import DeskServices
from resolvedesk.store import Store
from test_it_execution import model_for
from test_it_workflows import USER,SUPPORT,ACCESS,REPAIR
from test_resolvedesk import FakeEmbedding,response
from test_knowledge_governance import metadata,change


@pytest.fixture
def environment(tmp_path):
    store=Store(tmp_path/'contracts.sqlite3');BusinessService(store).seed_demo()
    kb=HybridKnowledge(store,FakeEmbedding())
    kb.add(SUPPORT,'policy','VPN Windows','# VPN\nVPN 检查网络，不代表企业真实制度。',metadata=metadata())
    change(kb)
    try:yield store,kb
    finally:store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fine,message',[
    ('greeting','您好！'),('asset_lookup','请查询我的资产和保修状态。'),
    ('ticket_status','请查看工单RD-000000000001的处理进度。'),
    ('device_hazard','设备冒烟，请转人工处理。'),
])
async def test_nonknowledge_tasks_are_not_blocked_by_managed_rag(environment,fine,message):
    store,kb=environment;model=model_for(fine=(fine,));services=DeskServices(store,kb,model,mcp_configs=[])
    async def insufficient(*args,**kwargs):
        return {'items':[],'mode':'hybrid','degraded':False,'candidate_count':1,'errors':[],'evidence_sufficient':False,'cached':False,'latency_ms':1}
    services.search=insufficient
    agent=DeskAgent(store,kb,model,services)
    try:
        run,_=store.create_run(USER,message,'no-rag-'+fine)
        await agent.execute(USER,run['id']);current=store.get_run(USER,run['id'])
        assert current['error'] not in {'insufficient_evidence','knowledge_unavailable'}
        assert '请补充产品、系统版本' not in current['answer']
        if fine=='device_hazard':assert any(e['kind']=='handoff_prepared' for e in store.events(USER,run['id']))
        assert not store.list_tickets(USER)
    finally:await agent.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('workflow,fields,message',[
    ('access_request',ACCESS,'请准备repo-resolvedesk read权限申请，使用七天，用途为阅读代码。'),
    ('device_repair',REPAIR,'请准备设备维修申请，本人LT-1001键盘失灵，已检查外接键盘仍未恢复。'),
    ('license_request',{'summary':'开发授权申请','description':'Java后端开发','category':'software','priority':'normal','product':'IntelliJ IDEA Ultimate','duration_days':7},'请准备IntelliJ IDEA Ultimate商业席位申请，用于Java后端开发，使用七天。'),
])
@pytest.mark.parametrize('wrong',['omitted','generic','other_workflow'])
async def test_model_cannot_downgrade_or_change_confirmed_request_type(environment,workflow,fields,message,wrong):
    store,kb=environment;model=model_for(fine=(workflow,));original=model.messages.create.side_effect
    supplied=fields|({'workflow':'generic'} if wrong=='generic' else {'workflow':'device_repair' if workflow!='device_repair' else 'access_request'} if wrong=='other_workflow' else {})
    if wrong=='omitted':supplied.pop('workflow',None)
    async def create(**kwargs):
        if kwargs.get('system','').startswith('你是 ResolveDesk'):return response(tool='prepare_ticket',args=supplied)
        return await original(**kwargs)
    model.messages.create.side_effect=create
    services=DeskServices(store,kb,model,mcp_configs=[]);agent=DeskAgent(store,kb,model,services)
    try:
        run,_=store.create_run(USER,message,'type-'+workflow+'-'+wrong)
        await agent.execute(USER,run['id']);current=store.get_run(USER,run['id'])
        assert current['status']=='awaiting_confirmation',current['answer']
        assert current['draft']['workflow']==workflow
        ticket=store.confirm(USER,run['id'],current['approval'])
        assert BusinessService(store).get(USER,ticket['id'])['kind']==workflow
    finally:await agent.close()


def test_new_missing_metadata_import_is_draft_and_can_be_completed(environment):
    _,kb=environment
    kb.add(SUPPORT,'new-unreviewed','未审核VPN','# VPN\nVPN new')
    row=next(r for r in kb.publications(SUPPORT) if r['document_id']=='new-unreviewed')
    assert row['state']=='draft' and row['metadata_complete'] is False
    assert not any(i['document_id']=='new-unreviewed' for i in kb.search(USER,'VPN')['items'])
    with pytest.raises(ValueError):kb.publication_action(SUPPORT,row['document_id'],'publish',row['version'],row['revision'],'not reviewed')
    kb.publication_action(SUPPORT,row['document_id'],'metadata',row['version'],row['revision'],'complete source',metadata())
    row=next(r for r in kb.publications(SUPPORT) if r['document_id']=='new-unreviewed')
    assert row['metadata_complete'] is True
    kb.publication_action(SUPPORT,row['document_id'],'publish',row['version'],row['revision'],'source checked')
    assert any(i['document_id']=='new-unreviewed' for i in kb.search(USER,'VPN')['items'])


def test_publication_changes_invalidate_measurement_identity_but_engine_changes_do_not():
    judge=SimpleNamespace(JUDGE_PROMPT='fixed',few_shot_prompt=lambda:'fixed')
    original={'dataset':{'version':'fixed'},'backend':'postgresql','evidence':[{'document_id':'doc','content':'same'}],
        'publications':[{'document_id':'doc','content_version':'one','state':'published','metadata':metadata()}],'engine_sources':{'agent.py':'one'}}
    changed=deepcopy(original);changed['publications'][0]['metadata']['applicability']=['Windows 10']
    assert suite_identity(original,1,True,'e5',judge)!=suite_identity(changed,1,True,'e5',judge)
    changed=deepcopy(original);changed['engine_sources']['agent.py']='two'
    assert suite_identity(original,1,True,'e5',judge)==suite_identity(changed,1,True,'e5',judge)


@pytest.mark.asyncio
async def test_plain_text_request_for_missing_fields_persists_waiting_state(environment):
    store,kb=environment;model=model_for(fine=('access_request',));original=model.messages.create.side_effect
    async def create(**kwargs):
        if kwargs.get('system','').startswith('你是 ResolveDesk'):return response('请提供资源、权限级别和使用天数。')
        return await original(**kwargs)
    model.messages.create.side_effect=create
    agent=DeskAgent(store,kb,model,DeskServices(store,kb,model,mcp_configs=[]))
    try:
        run,_=store.create_run(USER,'我要申请仓库权限，请先询问需要补充什么。','missing-turn-state')
        await agent.execute(USER,run['id'])
        assert store.get_run(USER,run['id'])['status']=='awaiting_input'
        assert not store.list_tickets(USER)
    finally:await agent.close()
