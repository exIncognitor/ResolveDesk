"""Real Flash + current ASGI/PG/Redis/Chroma monitor acceptance in an owned scope."""
import argparse
import asyncio
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
from types import SimpleNamespace
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true')
    if not parser.parse_args().live:parser.error('Use --live for the authorized budgeted Flash probe')
    from dotenv import load_dotenv
    import httpx
    from prometheus_client.parser import text_string_to_metric_families
    from core.paid_api import BudgetedAnthropic
    from agents.agent_orchestrator import AgentType
    from resolvedesk.settings import ServiceSettings
    from resolvedesk.store import Principal,Store
    from resolvedesk.retrieval import HybridKnowledge,LocalEmbedding
    from resolvedesk.coordination import Coordination
    from resolvedesk.vectors import ChromaVectors
    from resolvedesk.services import DeskServices
    from resolvedesk.agent import DeskAgent
    from resolvedesk.app import create_app
    from tools.migrate_services import TABLES
    load_dotenv(ROOT/'.env',override=False)
    os.environ.pop('RESOLVEDESK_ALERT_WEBHOOK',None)  # This isolated probe sends no external notifications.
    identity=uuid.uuid4().hex;schema='verify_monitor_'+identity;collection='verify-monitor-'+identity
    settings=ServiceSettings.load();store=Store(settings.database_url,schema=schema)
    embedding=LocalEmbedding(ROOT/'data/models/multilingual-e5-small')
    vectors=ChromaVectors(settings.chroma_url,embedding.identity,collection=collection)
    cache=Coordination(settings.redis_url,'verify-monitor-'+identity)
    kb=HybridKnowledge(store,embedding,vectors)
    people={'user':Principal('demo','learner'),'support':Principal('demo','support','support'),'peer':Principal('demo','peer')}
    for path in sorted((ROOT/'knowledge').glob('*.md')):kb.add(people['support'],path.stem,path.stem,path.read_text(encoding='utf-8'))
    model=BudgetedAnthropic(api_key=os.getenv('ANTHROPIC_API_KEY',''),base_url='https://api.deepseek.com/anthropic',timeout=25)
    before=model.ledger().summary()
    services=DeskServices(store,kb,model,coordination=cache)
    agent=DeskAgent(store,kb,model,services,cache)
    tokens={name:secrets.token_urlsafe(32) for name in people}
    accounts={hashlib.sha256(tokens[name].encode()).hexdigest():vars(p) for name,p in people.items()}
    api=create_app(store,kb,agent,accounts)
    checks={};report={'kind':'current_monitor_runtime','simulation':True,'checks':checks,'schema':schema}
    memory_name=None
    try:
        await services.start();memory_name=services.memory_vectors.name
        async with api.router.lifespan_context(api),httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://monitor.local',timeout=120) as http:
            async def request(method,path,body=None,actor='user'):
                result=await http.request(method,path,json=body,headers={'Authorization':'Bearer '+tokens[actor]})
                result.raise_for_status();return result.json()
            async def wait(run_id):
                async with asyncio.timeout(110):
                    while True:
                        result=await request('GET','/runs/'+run_id)
                        if result['status'] not in {'queued','running'}:return result
                        await asyncio.sleep(.05)
            first=await request('POST','/runs',{'message':'VPN 无法连接，同时 IntelliJ IDEA Ultimate 商业授权到期。只给只读处理建议，引用知识库，不提交工单。','request_key':'monitor-'+identity})
            first=await wait(first['id'])
            checks['real_composite_completes']=first['status']=='completed' and not first.get('error')
            async def injected(**kwargs):raise RuntimeError('synthetic_pre_call_technical_failure')
            for expert in services.roles._pool[AgentType.TECHNICAL]:expert._client=SimpleNamespace(messages=SimpleNamespace(create=injected))
            await request('POST',f'/runs/{first["id"]}/reply',{'message':'现在只讨论 VPN 连接超时的只读排障建议，请引用知识库，不要建工单。','request_key':'monitor-reply-'+identity})
            second=await wait(first['id'])
            checks['real_general_fallback_is_partial']=second['status']=='completed' and second.get('error')=='specialist_partial'
            gate=asyncio.Event();original_loop=agent._loop
            async def gated(p,run_id):
                if store.get_run(p,run_id)['message']=='合成暂停测试：在任何模型调用前等待':gate.set();await asyncio.Event().wait()
                else:await original_loop(p,run_id)
            agent._loop=gated
            cancelled=await request('POST','/runs',{'message':'合成暂停测试：在任何模型调用前等待','request_key':'monitor-cancel-'+identity})
            await asyncio.wait_for(gate.wait(),10)
            await request('POST',f'/runs/{cancelled["id"]}/pause')
            checks['free_pause_is_cancelled']=(await request('GET','/runs/'+cancelled['id']))['status']=='paused'
            # Let real background maintenance finish before closing its client.
            async with asyncio.timeout(60):
                while True:
                    with store.db() as db:
                        active=db.execute("SELECT COUNT(*) FROM memory_users WHERE state='running' OR (state='pending' AND next_attempt<=?)",(db.now(),)).fetchone()[0]
                    if not active:break
                    await asyncio.sleep(.1)
            observed=await request('GET','/monitor',actor='support')
            own=await request('GET','/monitor')
            peer=await request('GET','/monitor',actor='peer')
            checks['three_main_epochs_two_conversations']=observed['total_requests']==3 and observed['total_conversations']==2
            checks['main_success_partial_cancelled']=observed['run_stats']['desk']['status_counts']=={'success':1,'partial':1,'cancelled':1}
            checks['actual_failed_technical_observed']=observed['agent_stats']['technical']['status_counts'].get('failure')==1
            checks['actual_fallback_general_observed']=observed['agent_stats'].get('general',{}).get('success')==1
            checks['billing_observed_separately']=observed['agent_stats'].get('billing',{}).get('success')==1
            checks['owner_scope']=own['total_requests']==3 and not peer['agent_stats'] and peer['total_requests']==0
            checks['tools_have_real_circuit_state']=bool(observed['runtime_tools']) and all('circuit_state' in r['payload'] for r in observed['runtime_tools'])
            with store.db() as db:
                attempts=[dict(r) for r in db.execute('SELECT epoch,turn_key,status,error,instance,started,finished FROM execution_attempts ORDER BY started')]
                experts=[dict(r) for r in db.execute("SELECT name,instance,status,seconds FROM telemetry WHERE kind='agent' ORDER BY seq")]
            checks['durable_exact_epoch_keys']=len(attempts)==3 and len({(r['instance'],r['turn_key']) for r in attempts})==3
            checks['all_expert_times_measured']=len(experts)==4 and all(r['seconds']>=0 and r['instance']!='legacy' for r in experts)
            metric_response=await http.get('/metrics',headers={'Authorization':'Bearer '+tokens['support']});metric_response.raise_for_status()
            metric_samples=[s for f in text_string_to_metric_families(metric_response.text) for s in f.samples]
            checks['main_histogram_exact_count']=sum(s.value for s in metric_samples if s.name=='chat_latency_ms_count')==3
            checks['expert_histogram_exact_count']=sum(s.value for s in metric_samples if s.name=='agent_latency_ms_count')==4
            checks['no_business_write']=not store.list_tickets(people['user'])
            report.update(attempts=attempts,experts=experts,snapshot=observed,metrics=metric_response.text)
    except Exception as exc:report['error']=type(exc).__name__;checks['probe_execution']=False
    finally:
        await agent.close()
        report.update(budget_before=before,budget_after=model.ledger().summary(),passed=sum(checks.values()),total=len(checks))
        path=ROOT/'reports'/('monitor-runtime-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
        with path.open('x',encoding='utf-8') as dest:json.dump(report,dest,ensure_ascii=False,indent=2)
        print(json.dumps({'passed':report['passed'],'total':report['total'],'evidence':str(path.relative_to(ROOT)),'budget_after':report['budget_after']},ensure_ascii=False),flush=True)
        # Only this probe's UUID schema and collections are eligible for cleanup.
        with store.db(True) as db:
            tables=[r[0] for r in db.execute('SELECT tablename FROM pg_tables WHERE schemaname=?',(schema,))]
            if not re.fullmatch(r'verify_monitor_[a-f0-9]{32}',schema) or not set(tables)<=TABLES:raise RuntimeError('Unknown probe scope; preserving resources')
            for table in sorted(tables):db.execute('DROP TABLE "'+schema+'"."'+table+'"')
            db.execute('DROP SCHEMA "'+schema+'"')
        if memory_name:
            assert memory_name=='resolvedesk-memory-'+hashlib.sha256(collection.encode()).hexdigest()[:16]
            vectors.request('DELETE',vectors.base+'/'+memory_name)
        assert vectors.name==collection and collection.startswith('verify-monitor-')
        vectors.request('DELETE',vectors.base+'/'+collection)
        keys=[key async for key in cache.redis.scan_iter(match=cache.namespace+':*')]
        assert all(key.startswith(cache.namespace+':') for key in keys)
        if keys:await cache.redis.delete(*keys)
        vectors.close();store.close();await cache.close();await model.close()
    if not checks or not all(checks.values()):raise SystemExit(1)


if __name__=='__main__':asyncio.run(main())
