"""One budgeted Flash conversation verifies current traces in owned real services."""
import argparse
import asyncio
from datetime import datetime,timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true')
    if not parser.parse_args().live:parser.error('Use --live for the authorized budgeted Flash probe')
    logging.disable(logging.INFO)
    from dotenv import load_dotenv
    import httpx
    from core.paid_api import BudgetedAnthropic
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
    os.environ.pop('RESOLVEDESK_ALERT_WEBHOOK',None)
    identity=uuid.uuid4().hex;schema='verify_trace_'+identity;collection='verify-trace-'+identity
    settings=ServiceSettings.load();store=Store(settings.database_url,schema=schema)
    embedding=LocalEmbedding(ROOT/'data/models/multilingual-e5-small')
    vectors=ChromaVectors(settings.chroma_url,embedding.identity,collection=collection)
    cache=Coordination(settings.redis_url,'verify-trace-'+identity)
    kb=HybridKnowledge(store,embedding,vectors)
    user=Principal('demo','learner');support=Principal('demo','support','support');peer=Principal('demo','peer')
    kb.add(support,'vpn','VPN模拟制度','# VPN\n\n【合成模拟】VPN超时先检查普通网络，核对客户端版本并记录错误码。不要发送密码，通过现有IT渠道联系人员。')
    model=BudgetedAnthropic(api_key=os.getenv('ANTHROPIC_API_KEY',''),base_url='https://api.deepseek.com/anthropic',timeout=25)
    before=model.ledger().summary();services=DeskServices(store,kb,model,coordination=cache)
    agent=DeskAgent(store,kb,model,services,cache)
    tokens={p.user:secrets.token_urlsafe(32) for p in (user,support,peer)}
    accounts={hashlib.sha256(tokens[p.user].encode()).hexdigest():vars(p) for p in (user,support,peer)}
    app=create_app(store,kb,agent,accounts);checks={};report={'kind':'current_trace_runtime','simulation':True,'checks':checks}
    memory_name=None
    try:
        await services.start();memory_name=services.memory_vectors.name
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://localhost',timeout=120) as http:
            async def request(path,method='GET',body=None,actor=user):
                result=await http.request(method,path,json=body,headers={'Authorization':'Bearer '+tokens[actor.user]})
                result.raise_for_status();return result.json()
            run=await request('/runs','POST',{'message':'VPN连接超时。请专业节点先调用 mcp_support_support_checklist，category为network；主协调器再调用business_context核验本人可见目录，最后给只读建议并引用制度，不创建工单。诊断备注 token=synthetic-trace-secret（模拟占位）。','request_key':'trace-'+identity})
            async with asyncio.timeout(120):
                while True:
                    final=await request('/runs/'+run['id'])
                    if final['status'] not in {'queued','running'}:break
                    await asyncio.sleep(.1)
            first=await request('/trace/tool/'+run['id']+'?limit=3')
            trace=first['trace'];events=list(trace['events']);calls=list(trace['tool_calls']);cursor=trace['next_after']
            while cursor is not None:
                page=(await request('/trace/tool/'+run['id']+f'?limit=3&turn_key={trace["turn_key"]}&through={trace["snapshot_end"]}&after={cursor}'))['trace']
                events+=page['events'];calls+=page['tool_calls'];cursor=page['next_after']
            checks['completed_grounded']=final['status']=='completed' and not final.get('error') and '[C-' in final['answer']
            checks['native_input_and_output']=any(c['layer'] in {'main','bootstrap'} and c.get('input') and c.get('output_summary') for c in calls)
            checks['actual_specialist_mcp']=any(c['layer']=='specialist' and c.get('source')=='external_mcp' and c.get('mcp_server')=='support' and c.get('output_summary') for c in calls)
            checks['real_measured_tool_times']=bool(calls) and all(isinstance(c.get('latency_ms'),(float,int)) and c['latency_ms']>=0 for c in calls)
            checks['actual_main_time_in_receipt']=final.get('latency_ms',0)>0 and trace['latency_ms']==final['latency_ms'] and trace['timing_epoch']==1
            checks['no_duplicate_aggregate_calls']=len(calls)==len({c['trace_id'] for c in calls})
            checks['paginated_terminal_included']=any(e['kind']=='duration' for e in events) and len(events)>3 and len({e['seq'] for e in events})==len(events)
            recent=await request('/trace/tools?limit=1')
            checks['recent_turn_exact']=recent['items'][0]['request_id']==run['id'] and recent['items'][0]['turn_key']==run['turn_key']
            checks['peer_has_no_traces']=not (await request('/trace/tools',actor=peer))['items']
            denied=await http.get('/trace/tool/'+run['id'],headers={'Authorization':'Bearer '+tokens[peer.user]})
            checks['peer_trace_denied']=denied.status_code==404
            with store.db() as db:raw=''.join(r[0] for r in db.execute('SELECT payload FROM events WHERE run_id=?',(run['id'],)))
            checks['synthetic_credential_never_persisted']='synthetic-trace-secret' not in raw
            checks['no_business_write']=not store.list_tickets(user)
            async with asyncio.timeout(60):
                while True:
                    with store.db() as db:active=db.execute("SELECT COUNT(*) FROM memory_users WHERE state='running' OR (state='pending' AND next_attempt<=?)",(db.now(),)).fetchone()[0]
                    if not active:break
                    await asyncio.sleep(.1)
            report.update(trace={**trace,'events':events,'tool_calls':calls},final_status=final['status'])
    except Exception as exc:report['error_code']=type(exc).__name__;checks['probe_execution']=False
    finally:
        await agent.close()
        report.update(budget_before=before,budget_after=model.ledger().summary(),source_revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),passed=sum(checks.values()),total=len(checks))
        path=ROOT/'reports'/('trace-runtime-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
        with path.open('x',encoding='utf-8') as dest:json.dump(report,dest,ensure_ascii=False,indent=2)
        print(json.dumps({'passed':report['passed'],'total':report['total'],'evidence':str(path.relative_to(ROOT)),'budget_after':report['budget_after']}),flush=True)
        with store.db(True) as db:
            tables=[r[0] for r in db.execute('SELECT tablename FROM pg_tables WHERE schemaname=?',(schema,))]
            if not re.fullmatch(r'verify_trace_[a-f0-9]{32}',schema) or not set(tables)<=TABLES:raise RuntimeError('Unknown probe scope; preserving resources')
            for table in sorted(tables):db.execute('DROP TABLE "'+schema+'"."'+table+'"')
            db.execute('DROP SCHEMA "'+schema+'"')
        if memory_name:
            assert memory_name=='resolvedesk-memory-'+hashlib.sha256(collection.encode()).hexdigest()[:16]
            vectors.request('DELETE',vectors.base+'/'+memory_name)
        assert vectors.name==collection and collection.startswith('verify-trace-')
        vectors.request('DELETE',vectors.base+'/'+collection)
        keys=[key async for key in cache.redis.scan_iter(match=cache.namespace+':*')]
        assert all(key.startswith(cache.namespace+':') for key in keys)
        if keys:await cache.redis.delete(*keys)
        vectors.close();store.close();await cache.close();await model.close()
    if not checks or not all(checks.values()):raise SystemExit(1)


if __name__=='__main__':asyncio.run(main())
