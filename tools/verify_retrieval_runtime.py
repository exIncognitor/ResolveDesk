"""Real Flash optimization and fault/recovery through current retrieval + ASGI.

All documents, schema, users, caches and collections are isolated synthetic
fixtures. The only shared persistent state is the original cumulative budget.
The subprocess mode proves cache reuse across actual Python processes.
"""
import argparse
import asyncio
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


async def worker(config):
    if not config['schema'].startswith('verify_retrieval_') or not config['collection'].startswith('verify-retrieval-') or not config['namespace'].startswith('verify-retrieval-'):
        raise ValueError('Worker requires isolated verification scope')
    from core.paid_api import BudgetedAnthropic
    from resolvedesk.settings import ServiceSettings
    from resolvedesk.store import Store,Principal
    from resolvedesk.retrieval import LocalEmbedding,HybridKnowledge
    from resolvedesk.vectors import ChromaVectors
    from resolvedesk.coordination import Coordination
    from resolvedesk.services import DeskServices
    settings=ServiceSettings.load();store=Store(settings.database_url,schema=config['schema'])
    embedding=LocalEmbedding(ROOT/'data/models/multilingual-e5-small')
    vectors=ChromaVectors(settings.chroma_url,embedding.identity,collection=config['collection'])
    cache=Coordination(settings.redis_url,config['namespace'])
    client=BudgetedAnthropic(api_key='')  # Free retrieval only; no paid client credentials needed.
    service=DeskServices(store,HybridKnowledge(store,embedding,vectors),client,mcp_configs=[],coordination=cache)
    try:
        result=await service.search(Principal('demo','learner'),config['query'],optimize=False)
        print(json.dumps({'pid':os.getpid(),'cached':result['cached'],'layer':result['cache_layer'],'native_calls':service.manager._tools['it_rag'].stats.total}),flush=True)
    finally:
        await service.close();await client.close();await cache.close();vectors.close();store.close()


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true');parser.add_argument('--worker-config')
    args=parser.parse_args()
    if args.worker_config:return await worker(json.loads(args.worker_config))
    if not args.live:parser.error('Use --live for the authorized real Flash verification')
    from dotenv import load_dotenv
    import httpx
    from sqlalchemy import text
    from core.paid_api import BudgetedAnthropic
    from resolvedesk.settings import ServiceSettings
    from resolvedesk.store import Principal,Store
    from resolvedesk.retrieval import LocalEmbedding,HybridKnowledge
    from resolvedesk.vectors import ChromaVectors
    from resolvedesk.coordination import Coordination
    from resolvedesk.services import DeskServices
    from resolvedesk.agent import DeskAgent
    from resolvedesk.app import create_app
    load_dotenv(ROOT/'.env',override=False)
    identity=uuid.uuid4().hex;schema='verify_retrieval_'+identity;collection='verify-retrieval-'+identity
    settings=ServiceSettings.load();store=Store(settings.database_url,schema=schema)
    embedding=LocalEmbedding(ROOT/'data/models/multilingual-e5-small')
    vectors=ChromaVectors(settings.chroma_url,embedding.identity,collection=collection)
    cache=Coordination(settings.redis_url,'verify-retrieval-'+identity)
    kb=HybridKnowledge(store,embedding,vectors)
    user=Principal('demo','learner');support=Principal('demo','support','support')
    for index in range(8):kb.add(support,'vpn-'+str(index),'VPN排障制度'+str(index),'# VPN连接故障\n\n【合成模拟制度】VPN连接超时时先核对普通网络、记录错误码和客户端版本，再联系IT核验，不要提供密码。编号'+str(index))
    client=BudgetedAnthropic(api_key=os.getenv('ANTHROPIC_API_KEY',''),base_url='https://api.deepseek.com/anthropic',timeout=25)
    before=client.ledger().summary()
    services=DeskServices(store,kb,client,mcp_configs=[],coordination=cache)
    agent=DeskAgent(store,kb,client,services,cache)
    token=secrets.token_urlsafe(36)
    app=create_app(store,kb,agent,{hashlib.sha256(token.encode()).hexdigest():vars(user)})
    checks={};details={};memory_name=None
    try:
        query='VPN连接超时如何排查'
        optimized=await services.search(user,query,2,role='technical')
        checks['flash_rewrite_and_rerank']=bool(optimized['items']) and len(optimized['queries'])>1 and optimized['reranked'] and not optimized['errors'] and not optimized['degraded']
        after_first=client.ledger().summary()
        cached=await services.search(user,query,2,role='technical')
        checks['cache_precedes_paid_stages']=cached['cached'] and client.ledger().summary()==after_first
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://verify',headers={'Authorization':'Bearer '+token},timeout=120) as http:
            async def search(query,mode='hybrid'):
                response=await http.post('/knowledge/search',json={'query':query,'mode':mode});response.raise_for_status();return response.json()
            current=await search(query)
            configuration={'schema':schema,'collection':collection,'namespace':cache.namespace,'query':query}
            process=await asyncio.to_thread(subprocess.run,[sys.executable,'-X','utf8','-B',str(Path(__file__).absolute()),'--worker-config',json.dumps(configuration)],capture_output=True,text=True,timeout=40,check=True)
            child=json.loads(process.stdout)
            checks['cross_process_redis_cache']=child['pid']!=os.getpid() and child['cached'] and child['layer']=='redis' and child['native_calls']==0
            details['cache_worker']=child
            checks['free_api_and_child_do_not_spend']=client.ledger().summary()==after_first
            old=current['items'][0]
            kb.add(support,old['document_id'],old['title'],'# 私有新版\n\nVPN支持人员私有资料。',visibility='private')
            updated=await search(query)
            checks['reimport_acl_invalidates_cache']=not updated['cached'] and old['citation'] not in {i['citation'] for i in updated['items']} and not kb.validate_citations(user,[old['citation']])
            original=vectors.search
            def offline(*a,**kw):raise ConnectionError('synthetic transport outage')
            vectors.search=offline
            for index in range(5):
                degraded=await search('VPN 故障注入 '+str(index))
                checks['lexical_fallback_'+str(index)]=degraded['degraded'] and bool(degraded['items']) and degraded['effective_mode']=='bm25'
            tool=services.manager._tools['it_rag']
            checks['breaker_opens']=tool.breaker.state.value=='open'
            strict=await search('VPN 严格向量故障','vector')
            checks['strict_vector_unavailable']=strict['degraded'] and strict['items']==[] and strict['effective_mode']=='unavailable'
            vectors.search=original;tool.breaker.recovery_s=0
            recovered=await search('VPN 恢复查询')
            checks['real_chroma_probe_recovers']=not recovered['degraded'] and tool.breaker.state.value=='closed'
            await services.start();memory_name=services.memory_vectors.name
            run=await http.post('/runs',json={'message':'VPN连接超时，请依据制度给出排查建议，不要建单。','request_key':'verify-retrieval-'+identity})
            run.raise_for_status();run=run.json()
            async with asyncio.timeout(120):
                while True:
                    final=store.get_run(user,run['id'])
                    if final['status'] not in {'queued','running'}:break
                    await asyncio.sleep(.1)
            events=store.events(user,run['id'])
            retrieval=next((e['payload'] for e in events if e['kind']=='retrieval'),{})
            checks['current_agent_retrieval_path']=retrieval.get('effective_mode')=='hybrid' and not retrieval.get('degraded',True) and bool(retrieval.get('citations'))
            checks['current_agent_answers_with_evidence']=final['status']=='completed' and '[C-' in final['answer'] and not final.get('error')
            checks['no_business_write']=not store.list_tickets(user)
            details['final_status']=final['status'];details['retrieval']=retrieval
    finally:
        await agent.close()
        after=client.ledger().summary()
        report={'kind':'current_asgi_real_retrieval_reliability','simulation':True,'checks':checks,'details':details,
            'passed':sum(checks.values()),'total':len(checks),'budget_before':before,'budget_after':after,
            'source_revision':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()}
        evidence=ROOT/'reports'/('retrieval-runtime-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
        with evidence.open('x',encoding='utf-8') as dest:json.dump(report,dest,ensure_ascii=False,indent=2)
        print(json.dumps({'passed':report['passed'],'total':report['total'],'evidence':str(evidence.relative_to(ROOT))}),flush=True)
        from tools.migrate_services import TABLES
        with store.database.engine.begin() as db:
            tables=set(db.execute(text('SELECT tablename FROM pg_tables WHERE schemaname=:s'),{'s':schema}).scalars())
            if not schema.startswith('verify_retrieval_') or not tables<=TABLES:raise RuntimeError('Unexpected synthetic schema')
            db.exec_driver_sql('DROP SCHEMA "'+schema+'" CASCADE')
        if memory_name:
            if memory_name!='resolvedesk-memory-'+hashlib.sha256(collection.encode()).hexdigest()[:16]:raise RuntimeError('Unexpected memory collection')
            vectors.request('DELETE',vectors.base+'/'+memory_name)
        if vectors.name!=collection or not collection.startswith('verify-retrieval-'):raise RuntimeError('Unexpected vector scope')
        vectors.request('DELETE',vectors.base+'/'+collection);vectors.close();store.close()
        keys=[key async for key in cache.redis.scan_iter(match=cache.namespace+':*')]
        if any(not key.startswith(cache.namespace+':') for key in keys):raise RuntimeError('Unexpected fixture keys')
        if keys:await cache.redis.delete(*keys)
        await cache.close();await client.close()
    if not checks or not all(checks.values()):raise SystemExit(1)


if __name__=='__main__':asyncio.run(main())
