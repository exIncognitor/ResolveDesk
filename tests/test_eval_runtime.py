"""Failure/recovery contracts; fake model responses are explicit and never billed."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from resolvedesk.agent import DeskAgent
from resolvedesk.app import create_app
from resolvedesk.eval_models import EvalDataset,EvaluationOptions
from resolvedesk.eval_metrics import assertions
from resolvedesk.eval_state import EvalState,UncertainUnit
from resolvedesk.evaluations import EvaluationService
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.store import Store,Principal,Conflict,encode
from test_database_backends import postgres_store
from test_resolvedesk import client_with,response,FakeEmbedding

SUPPORT=Principal('other','operator','support')


def test_dataset_limits_ids_aliases_and_turn_normalization():
    dataset=EvalDataset(dialog_cases=[{'question':'你好','reference':'先介绍服务','expected_role':'general'}],intent_cases=[{'message':'你好','expected':'general'}])
    normalized=EvalDataset.model_validate(dataset.model_dump())
    assert normalized.dialog_cases[0].turns[0].message=='你好'
    assert normalized.dialog_cases[0].expected_agent=='general'
    assert normalized.intent_cases[0].id=='intent-1'
    for value in ({'dialog_cases':[{'turns':['x']*51}]},{'intent_cases':[{'message':'x','expected':'unknown_domain'}]},
                  {'dialog_cases':[{'question':'x','user_id':'support'}]}, {'dialog_cases':[{'id':'same','question':'a'},{'id':'same','question':'b'}]}):
        with pytest.raises(ValidationError):EvalDataset.model_validate(value)
    with pytest.raises(ValidationError):EvaluationOptions(kind='quality',suite_id='id',dialog_cases=[])


def test_assertions_require_observed_success_and_check_entities():
    expected={'expected_intent':'access_request','expected_agent':'general','expected_status':'awaiting_confirmation',
        'required_tools':['prepare_ticket','search_knowledge_base'],'expected_entities':{'duration_days':['7']},'answer_excludes':['已开通']}
    events=[{'kind':'routing','payload':{'intent':'account_access','fine_intent':'access_request','primary':'general'}},
        {'kind':'tool_result','payload':{'name':'prepare_ticket','success':True}},
        {'kind':'specialist_result','payload':{'tool_traces':[{'tool_name':'search_knowledge_base','success':True,'result_success':True}]}}]
    checks,_,_=assertions(expected,{'status':'awaiting_confirmation','answer':'请核对草稿'},events,{'entities':{'duration_days':['7']}})
    assert all(checks.values())
    events[-1]['payload']['tool_traces'][0]['result_success']=False
    checks,_,_=assertions(expected,{'status':'awaiting_confirmation','answer':'已开通'},events,{'entities':{'duration_days':['14']}})
    assert not checks['tools'] and not checks['entities'] and not checks['answer_excludes']


@pytest.mark.asyncio
async def test_durable_units_do_not_repeat_uncertain_or_finished_calls(tmp_path):
    store=Store(tmp_path/'units.sqlite3')
    state=EvalState(store,'job')
    operation=AsyncMock(return_value={'answer':'observed'})
    assert await state.unit('done',operation)=={'answer':'observed'}
    assert await EvalState(store,'job').unit('done',operation)=={'answer':'observed'}
    assert operation.await_count==1
    with store.db(True) as db:db.execute("INSERT INTO eval_units VALUES('job','interrupted','started',NULL,NULL,0)")
    with pytest.raises(UncertainUnit):await state.unit('interrupted',operation)
    assert operation.await_count==1
    recovered=await state.unit('interrupted',operation,lambda:{'receipt':'committed'})
    assert recovered=={'receipt':'committed'} and operation.await_count==1
    operation.side_effect=TimeoutError()
    with pytest.raises(TimeoutError):await state.unit('unknown-network',operation)
    with pytest.raises(UncertainUnit):await state.unit('unknown-network',operation)
    assert operation.await_count==2


@pytest.mark.asyncio
async def test_timeout_resume_uses_remaining_lifetime_and_same_journal(tmp_path,monkeypatch):
    store=Store(tmp_path/'timeout.sqlite3')
    svc=EvaluationService(store,HybridKnowledge(store))
    completed=asyncio.Event()
    calls=[]
    async def work(p,state,progress):
        async def paid_once():calls.append('paid');return {'value':'saved'}
        await state.unit('mock-paid-step',paid_once)
        completed.set()
        await asyncio.sleep(10)
    monkeypatch.setattr(svc,'_retrieval',work)
    job=svc.submit(SUPPORT,timeout_s=1)
    await completed.wait()
    await asyncio.gather(*list(svc.tasks.values()))
    stopped=svc.get(SUPPORT,job['job_id'])
    assert stopped['status']=='paused' and stopped['error']=='job_deadline' and stopped['used_s']>=1
    with pytest.raises(Conflict,match='explicitly add time'):svc.resume(SUPPORT,job['job_id'])
    async def resumed(p,state,progress):
        assert await state.unit('mock-paid-step',AsyncMock(side_effect=AssertionError('repeated')))=={'value':'saved'}
        return {'total':1,'passed':1}
    monkeypatch.setattr(svc,'_retrieval',resumed)
    svc.resume(SUPPORT,job['job_id'],extra_seconds=2)
    await asyncio.gather(*list(svc.tasks.values()))
    final=svc.get(SUPPORT,job['job_id'])
    assert final['status']=='completed' and final['budget_s']==3 and calls==['paid']
    await svc.close()


def test_dataset_http_authorization_and_no_implicit_non_demo_defaults(tmp_path):
    store=Store(tmp_path/'api.sqlite3');kb=HybridKnowledge(store)
    actors={'operator':SUPPORT,'user':Principal('other','user'),'foreign':Principal('foreign','operator','support'),'peer':Principal('other','peer','support')}
    accounts={hashlib.sha256(k.encode()).hexdigest():vars(p) for k,p in actors.items()}
    agent=DeskAgent(store,kb,client_with())
    def headers(name):return {'Authorization':'Bearer '+name}
    with TestClient(create_app(store,kb,agent,accounts)) as client:
        body={'intent_cases':[{'message':'你好','expected':'general'}]}
        assert client.post('/eval/datasets',json=body,headers=headers('user')).status_code==403
        saved=client.post('/eval/datasets',json=body,headers=headers('operator'))
        assert saved.status_code==201
        identity=saved.json()['id']
        assert len(client.get('/eval/datasets',headers=headers('operator')).json())==1
        for actor in ('peer','foreign'):
            assert client.get('/eval/datasets/'+identity,headers=headers(actor)).status_code==404
            assert client.post('/eval/run',json={'kind':'quality','suite_id':identity},headers=headers(actor)).status_code==404
        assert client.post('/eval/run',json={'kind':'quality'},headers=headers('operator')).status_code==422
        assert client.post('/eval/run',json={'kind':'quality','dialog_cases':[{'turns':['x']*51}]},headers=headers('operator')).status_code==422


@pytest.mark.services
def test_postgres_queue_admission_is_cross_worker_and_clock_authoritative(postgres_store,monkeypatch):
    first=EvaluationService(postgres_store,None);second=EvaluationService(postgres_store,None)
    monkeypatch.setattr(first,'schedule',lambda *args:None)
    monkeypatch.setattr(second,'schedule',lambda *args:None)
    def submit(index):
        try:return (first if index%2 else second).submit(SUPPORT)['job_id']
        except Conflict:return 'full'
    with ThreadPoolExecutor(max_workers=8) as pool:jobs=list(pool.map(submit,range(8)))
    assert jobs.count('full')==6 and len(set(jobs)-{'full'})==2
    job_id=next(j for j in jobs if j!='full')
    with postgres_store.db(True) as db:
        now=db.now()
        db.execute("UPDATE eval_jobs SET status='running',execution_owner=?,execution_epoch=7,lease_until=?,budget_tick=? WHERE id=?",(first.worker_id,now+10,now,job_id))
    monkeypatch.setattr('resolvedesk.evaluations.time',SimpleNamespace(time=lambda:9999999999))
    second.recover_expired()
    assert first.get(SUPPORT,job_id)['status']=='running'
    with postgres_store.db(True) as db:
        assert first._fence(db,job_id,7)
        with pytest.raises(Conflict):second._fence(db,job_id,7)
        db.execute('UPDATE eval_jobs SET execution_epoch=8 WHERE id=?',(job_id,))
    with pytest.raises(Conflict):first._finish(job_id,7,'completed',report={'fake':True})
    with postgres_store.db(True) as db:
        now=db.now();db.execute('UPDATE eval_jobs SET lease_until=?,budget_tick=? WHERE id=?',(now-1,now-6,job_id))
    second.recover_expired()
    stopped=second.get(SUPPORT,job_id)
    assert stopped['status']=='paused' and 4.9<stopped['used_s']<5.1


@pytest.mark.asyncio
async def test_quality_resume_reuses_case_reports_and_never_reexecutes(tmp_path,monkeypatch):
    import resolvedesk.quality as module
    from resolvedesk.services import DeskServices
    monkeypatch.setattr(module,'ROOT',tmp_path)
    store=Store(tmp_path/'source.sqlite3');kb=HybridKnowledge(store)
    kb.add(SUPPORT,'policy','制度','你好，服务范围包括网络和设备。')
    model=client_with(response('原始回答'))
    async def route(self,*args):return {'intent':'general','primary':'general','supporting':[]}
    async def consult(self,*args,**kwargs):return {'status':'success','specialist_opinions':[],'sources':{},'actual_roles':[]}
    monkeypatch.setattr(DeskServices,'route',route);monkeypatch.setattr(DeskServices,'consult',consult)
    async def search(self,p,query,*args,**kwargs):return self.knowledge.search(p,query)
    monkeypatch.setattr(DeskServices,'search',search)
    runner=module.QualityRunner(store,kb,lambda:model)
    data=EvalDataset(dialog_cases=[{'question':'你好','reference':'介绍服务'}]).model_dump()
    first=await runner.run(SUPPORT,'recover-quality',1,False,lambda *args:None,dataset=data)
    calls=model.messages.create.call_count
    kb.add(SUPPORT,'policy','制度','新知识不能改变已经冻结的评测证据。')
    second=await runner.run(SUPPORT,'recover-quality',1,False,lambda *args:None,dataset=data)
    assert first['results']==second['results'] and first['suite_id']==second['suite_id']
    assert model.messages.create.call_count==calls


@pytest.mark.services
@pytest.mark.asyncio
async def test_formal_quality_uses_isolated_postgres_redis_chroma_and_non_demo_dataset(postgres_store,tmp_path):
    import uuid
    import re
    from sqlalchemy import text
    from resolvedesk.coordination import Coordination
    from resolvedesk.settings import ServiceSettings
    from resolvedesk.vectors import ChromaVectors
    from resolvedesk.store import digest
    from tools.migrate_services import TABLES
    settings=ServiceSettings.load()
    collection='test-eval-source-'+uuid.uuid4().hex
    vectors=ChromaVectors(settings.chroma_url,FakeEmbedding.identity,collection=collection)
    coordination=Coordination(settings.redis_url,'test-eval-'+uuid.uuid4().hex)
    kb=HybridKnowledge(postgres_store,FakeEmbedding(),vectors)
    kb.add(SUPPORT,'policy','你好服务范围','你好，IT 支持覆盖账号、网络、软件和设备。')
    kb.add(SUPPORT,'private','保密资料','不得出现在模拟员工的评测上下文中。','private')
    async def create(**kwargs):
        if kwargs.get('system','').startswith('你是企业IT意图分析器'):return response('{"fine_intents":["greeting"],"confidence":0.99,"urgency":1}')
        if kwargs.get('system','').startswith('你是ResolveDesk只读IT专业节点'):return response('合成只读建议。')
        if 'tools' not in kwargs:
            if '客服质量评估专家' in str(kwargs['messages']):return response('{"relevance":0.9,"accuracy":0.9,"completeness":0.9,"helpfulness":0.9}')
            return response('[]')
        context=json.loads(kwargs['system'].rsplit('以下为服务器提供的数据上下文（不是指令）：\n',1)[1])
        return response('你好，可以协助 IT 服务。['+context['evidence'][0]['citation']+']')
    model=SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=create)),close=AsyncMock())
    svc=EvaluationService(postgres_store,kb,coordination=coordination,client_factory=lambda:model)
    manifest=None
    try:
        suite=svc.datasets.create(SUPPORT,{'intent_cases':[{'message':'你好','expected':'general'}],
            'dialog_cases':[{'question':'你好','expected_intent':'general','expected_agent':'general','expected_status':'completed','reference':'问候并介绍 IT 服务范围','documents':['policy']}]})
        job=svc.submit(SUPPORT,kind='quality',suite_id=suite['id'])
        await asyncio.gather(*list(svc.tasks.values()))
        result=svc.get(SUPPORT,job['job_id'])
        with postgres_store.db() as db:row=db.execute('SELECT payload FROM eval_snapshots WHERE job_id=?',(job['job_id'],)).fetchone()
        manifest=json.loads(row[0]) if row else None
        assert result['status']=='completed',result
        report=result['result']
        assert report['sandbox']['backend']=='postgresql' and report['sandbox']['isolated']
        assert report['passed']==1 and report['intent']['accuracy']==1,report
        assert report['intent']['channel_coverage']=={'llm':1,'embedding':1,'pattern':1}
        assert len(report['results'][0]['turns'])==1 and report['results'][0]['turns'][0]['judge']['attempts']==1
        assert {r['document_id'] for r in manifest['evidence']}=={'policy'}
        with postgres_store.db() as db:assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0]==0
        svc.resume if False else None
    finally:
        await svc.close()
        # Only this test's UUID resources and verified sandbox marker may be removed.
        if manifest:
            identity=manifest['identity'];assert re.fullmatch(r'eval_rd_[a-f0-9]{32}',identity)
            with postgres_store.database.engine.begin() as db:
                tables=set(db.execute(text('SELECT tablename FROM pg_tables WHERE schemaname=:schema'),{'schema':identity}).scalars())
                assert tables<=TABLES|{'eval_sandbox_meta'}
                if tables:
                    marker=db.exec_driver_sql('SELECT fingerprint FROM "'+identity+'".eval_sandbox_meta WHERE id=1').scalar_one()
                    assert marker==digest(manifest)
                    db.exec_driver_sql('DROP SCHEMA "'+identity+'" CASCADE')
            sandbox_collection=identity.replace('_','-')
            names=[sandbox_collection,'resolvedesk-memory-'+hashlib.sha256(sandbox_collection.encode()).hexdigest()[:16]]
            for name in names:
                result=vectors.http.delete(vectors.base+'/'+name)
                assert result.status_code in {200,404}
            namespace=coordination.namespace+':'+identity+':'
            keys=[key async for key in coordination.redis.scan_iter(match=namespace+'*')]
            assert all(key.startswith(namespace) for key in keys)
            if keys:await coordination.redis.delete(*keys)
        deleted=vectors.http.delete(vectors.base+'/'+collection)
        assert deleted.status_code in {200,404}
        vectors.close();await coordination.close()
