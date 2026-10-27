"""G09 measures actual epochs/instances; all model replies are explicit fixtures."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json

import httpx
from prometheus_client.parser import text_string_to_metric_families
import pytest

from resolvedesk.agent import DeskAgent
from resolvedesk.metrics import BUCKETS
from resolvedesk.monitoring import Monitor
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.services import DeskServices
from resolvedesk.store import Store,Principal,Conflict
from test_database_backends import postgres_store
from test_resolvedesk import USER,OTHER,SUPPORT,client_with
from test_it_execution import environment,model_for


def samples(text):
    return [sample for family in text_string_to_metric_families(text) for sample in family.samples]


@pytest.mark.services
def test_durable_histograms_are_exact_deduplicated_and_survive_retention(postgres_store):
    from resolvedesk.retention import Retention
    store=postgres_store;HybridKnowledge(store);monitor=Monitor(store)
    for index,(ok,seconds,status) in enumerate([(True,.01,'success'),(False,1.,'failure'),(True,.5,'partial'),(True,None,'success')]):
        assert monitor.record('demo','agent','technical',ok,seconds,instance='node-a',owner='alice',status=status,event_key=str(index))
        assert not monitor.record('demo','agent','technical',ok,seconds,instance='node-a',owner='alice',status=status,event_key=str(index))
    exported=samples(monitor.prometheus(SUPPORT))
    histogram=[s for s in exported if s.name=='agent_latency_ms_count']
    assert sum(s.value for s in histogram)==3
    assert sum(s.value for s in exported if s.name=='agent_latency_ms_sum')==1510
    assert sum(s.value for s in exported if s.name=='agent_latency_ms_bucket' and s.labels['le']=='500')==2
    assert sum(s.value for s in exported if s.name=='resolvedesk_executions_total')==4
    assert monitor.snapshot(USER)['agent_stats']['technical']['status_counts']['partial']==1
    assert monitor.snapshot(OTHER)['agent_stats']=={}
    with store.db(True) as db:db.execute('UPDATE telemetry SET created=1')
    Retention(store).sweep(SUPPORT,days=7,apply=True)
    restarted=Monitor(store)
    cumulative=samples(restarted.prometheus(SUPPORT))
    assert sum(s.value for s in cumulative if s.name=='agent_latency_ms_count')==3
    assert sum(s.value for s in cumulative if s.name=='resolvedesk_executions_total')==4
    assert not restarted.refresh('other')['agent_stats']


@pytest.mark.services
def test_concurrent_duplicate_observation_only_increments_once(postgres_store):
    monitor=Monitor(postgres_store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        inserted=list(pool.map(lambda _:monitor.record('demo','agent','technical',True,.1,event_key='one-observed-call'),range(8)))
    assert sum(inserted)==1
    assert monitor.refresh('demo')['agent_stats']['technical']['total']==1
    assert sum(s.value for s in samples(monitor.prometheus(SUPPORT)) if s.name=='agent_latency_ms_count')==1


def test_zscore_uses_previous_samples_and_persistent_alerts_do_not_multiply(tmp_path):
    store=Store(tmp_path/'zscore.sqlite3');monitor=Monitor(store)
    for _ in range(30):monitor.record('demo','agent','technical',True,1.,instance='node-a')
    assert not monitor.refresh('demo')['active_alerts']
    monitor.record('demo','agent','technical',True,5.,instance='node-a')
    first=monitor.refresh('demo')
    assert first['agent_stats']['technical']['anomalies'][0]['z_score']>2.5
    assert first['agent_stats']['technical']['anomalies'][0]['baseline_samples']==30
    assert any(a['payload']['rule']=='z_score' for a in first['active_alerts'])
    count=len(first['notifications'])
    for _ in range(3):assert len(Monitor(store).refresh('demo')['notifications'])==count
    monitor.record('demo','agent','technical',True,1.,instance='node-a')
    assert not monitor.refresh('demo')['active_alerts']


def test_main_epoch_outcome_cannot_be_relabelled_by_a_new_turn(tmp_path):
    store=Store(tmp_path/'epochs.sqlite3');monitor=Monitor(store)
    run,_=store.create_run(USER,'first','first-turn-key')
    first=store.claim_execution(USER,run['id'],'owner-a',instance='worker-a')
    with store.execution(first):store.finish(USER,run['id'],'completed','partial answer','specialist_partial')
    old=store.get_turn(USER,run['id'],'first-turn-key')
    assert store.events(USER,run['id'])[-1]['kind']=='duration'
    assert old['event_end']==store.events(USER,run['id'])[-1]['seq']
    store.reply(USER,run['id'],'second','second-turn-key')
    second=store.claim_execution(USER,run['id'],'owner-b',instance='worker-b')
    with store.execution(first),pytest.raises(Conflict):store.finish(USER,run['id'],'failed','stale result')
    with store.execution(second):store.finish(USER,run['id'],'completed','second answer')
    result=monitor.snapshot(SUPPORT)
    assert result['total_requests']==2 and result['total_conversations']==1
    assert result['run_stats']['desk']['status_counts']=={'partial':1,'success':1}
    with store.db() as db:
        rows=db.execute('SELECT epoch,turn_key,status FROM execution_attempts ORDER BY epoch').fetchall()
        assert [(r['epoch'],r['turn_key'],r['status']) for r in rows]==[(1,'first-turn-key','partial'),(2,'second-turn-key','completed')]


def test_metric_projection_failure_preserves_answer_and_retries_without_duplicates(tmp_path,monkeypatch):
    import resolvedesk.metrics as metrics
    store=Store(tmp_path/'projection.sqlite3');monitor=Monitor(store)
    run,_=store.create_run(USER,'test','metrics-outage')
    store.claim(USER,run['id'])
    original=metrics.write_sample
    monkeypatch.setattr(metrics,'write_sample',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('synthetic counter outage')))
    store.finish(USER,run['id'],'completed','committed answer')
    assert store.get_run(USER,run['id'])['answer']=='committed answer'
    with store.db() as db:
        row=db.execute('SELECT * FROM execution_attempts').fetchone()
        assert row['finished'] is not None and row['projected']==0
    monkeypatch.setattr(metrics,'write_sample',original)
    assert monitor.refresh('demo')['total_requests']==1
    assert monitor.refresh('demo')['total_requests']==1


@pytest.mark.asyncio
async def test_actual_fallback_records_each_instance_and_main_partial(environment):
    store,kb=environment;model=model_for(failing=('technical',))
    svc=DeskServices(store,kb,model,mcp_configs=[]);agent=DeskAgent(store,kb,model,svc)
    run,_=store.create_run(USER,'VPN连接超时','monitor-real-fallback')
    try:
        await agent.execute(USER,run['id'])
        result=agent.monitor.snapshot(SUPPORT)
        assert result['agent_stats']['technical']['total']==1
        assert result['agent_stats']['technical']['success']==0
        assert result['agent_stats']['general']['success']==1
        assert result['run_stats']['desk']['status_counts']=={'partial':1}
        assert result['total_requests']==1
        with store.db() as db:
            rows=db.execute("SELECT instance,seconds FROM telemetry WHERE kind='agent'").fetchall()
            assert len({r['instance'] for r in rows})==2 and all(r['seconds']>=0 for r in rows)
    finally:await agent.close()


@pytest.mark.asyncio
async def test_latency_penalty_changes_actual_routing_and_fast_probe_recovers(environment):
    store,kb=environment;model=model_for()
    svc=DeskServices(store,kb,model,mcp_configs=[])
    svc.monitor.policy['agent_avg_ms']=10;svc.probe_cooldown=.01
    run,_=store.create_run(USER,'VPN连接超时','slow-routing');store.claim(USER,run['id'])
    try:
        for _ in range(3):
            routed=await svc.route(USER,run['id'],run['message'],[],{})
            await svc.consult(USER,run['id'],run['message'],[],{'evidence':[]},routed)
        stats=svc.monitor.refresh('demo')
        assert stats['agent_stats']['technical']['avg_ms']>10
        routed=await svc.route(USER,run['id'],run['message'],[],{})
        assert routed['primary']=='general' and routed['monitor_penalties']['technical']>=.5
        # Real fast fixture response, retaining the same role instance and call path.
        async def fast(**kwargs):
            from test_resolvedesk import response
            return response('合成只读建议。')
        for agent in svc.roles._pool[next(k for k in svc.roles._pool if k.value=='technical')]:agent._client=type('Model',(),{'messages':type('Messages',(),{'create':staticmethod(fast)})()})()
        await asyncio.sleep(.02)
        probe=await svc.route(USER,run['id'],run['message'],[],{})
        assert probe['primary']=='technical' and probe['recovery_probe']
        await svc.consult(USER,run['id'],run['message'],[],{'evidence':[]},probe)
        assert (await svc.route(USER,run['id'],run['message'],[],{}))['primary']=='technical'
    finally:await svc.close()


@pytest.mark.asyncio
async def test_real_manager_breaker_and_cache_observations_are_tenant_scoped(tmp_path):
    from tooling.tool_manager import Tool
    store=Store(tmp_path/'tools.sqlite3');svc=DeskServices(store,HybridKnowledge(store),client_with(),mcp_configs=[])
    async def fail(*args):raise RuntimeError('synthetic dependency failure')
    svc.manager.register(Tool('broken','test',fail,{'type':'object'},cache_ttl=0,timeout_s=.1))
    context={'tenant':'demo','user_id':'alice','role':'user'}
    try:
        for _ in range(5):assert not (await svc.manager.call('broken',{},context)).success
        report=svc.monitor.refresh('demo')
        assert report['tool_stats']['broken']['consecutive_fails']==5
        row=next(r for r in report['runtime_tools'] if r['name']=='broken')
        assert row['payload']['circuit_state']=='open'
        assert all(k not in row['payload'] for k in ('total','success_rate','avg_latency_ms'))
        assert svc.monitor.refresh('foreign')['runtime_tools']==[]
        assert any('熔断' in s['title'] for s in report['suggestions'])
    finally:await svc.close()


@pytest.mark.services
@pytest.mark.asyncio
async def test_two_monitor_workers_claim_one_notification(postgres_store,monkeypatch):
    first=Monitor(postgres_store);second=Monitor(postgres_store)
    for _ in range(3):first.record('demo','tool','broken',False,.01)
    first.refresh('demo');calls=[]
    async def receive(request):calls.append(request.headers['Idempotency-Key']);await asyncio.sleep(.04);return httpx.Response(200)
    original=httpx.AsyncClient
    monkeypatch.setenv('RESOLVEDESK_ALERT_WEBHOOK','http://local-fixture/alerts')
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(receive),**kw))
    await asyncio.gather(first.deliver(),second.deliver())
    assert len(calls)==1
    with postgres_store.db() as db:
        row=db.execute('SELECT state,attempts FROM alert_outbox').fetchone()
        assert row['state']=='delivered' and row['attempts']==1
