"""Budgeted Flash + E5/Chroma/Redis/PG memory through the current ASGI entry.

Uses a fresh synthetic application schema/collections. Finance remains the
single original cumulative authority. No real approvals or external messages.
"""
import argparse
import asyncio
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true')
    if not parser.parse_args().live:parser.error('Use --live for the authorized real Flash probe')
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
    from resolvedesk.business import BusinessService
    load_dotenv(ROOT/'.env',override=False)
    identity=uuid.uuid4().hex;schema='verify_memory_'+identity;collection='verify-memory-'+identity
    settings=ServiceSettings.load()
    store=Store(settings.database_url,schema=schema)
    embedding=LocalEmbedding(ROOT/'data/models/multilingual-e5-small')
    vectors=ChromaVectors(settings.chroma_url,embedding.identity,collection=collection)
    cache=Coordination(settings.redis_url,'verify-memory-'+identity)
    kb=HybridKnowledge(store,embedding,vectors)
    user=Principal('demo','learner');support=Principal('demo','support','support')
    for source in sorted((ROOT/'knowledge').glob('*.md')):
        content=source.read_text(encoding='utf-8');kb.add(support,source.stem,content.splitlines()[0].lstrip('# '),content)
    BusinessService(store).seed_demo()
    model=BudgetedAnthropic(api_key=os.getenv('ANTHROPIC_API_KEY',''),base_url='https://api.deepseek.com/anthropic',timeout=25)
    before=model.ledger().summary()
    services=DeskServices(store,kb,model,coordination=cache)
    agent=DeskAgent(store,kb,model,services,cache)
    token=secrets.token_urlsafe(36)
    app=create_app(store,kb,agent,{hashlib.sha256(token.encode()).hexdigest():vars(user)})
    checks={};memory_name=None
    try:
        old,_=store.create_run(user,'我使用 Windows 11 和 IntelliJ IDEA，偏好先给命令再解释。企业VPN连接超时，错误码619。','verify-memory-history')
        for index in range(7):
            store.claim(user,old['id']);store.finish(user,old['id'],'completed','VPN 仍待排查；已尝试检查本地网络 '+str(index))
            if index<6:store.reply(user,old['id'],'VPN 仍连接失败，补充排查记录 '+str(index))
        original=store.history(user,old['id'])
        # Foreground must bridge old messages without a new paid reservation.
        immediate=await services.memory.context(user,old['id'])
        checks['foreground_no_paid_call']=model.ledger().summary()==before
        checks['extractive_bridge']=bool(immediate['summary'] and immediate['working_messages'])
        await services.start();memory_name=services.memory_vectors.name
        async with asyncio.timeout(60):
            while True:
                info=services.memory.inspect(user)
                if info['maintenance'] and info['maintenance']['state']=='idle' and services.memory.drain_index(4)['failed']==0:break
                await asyncio.sleep(.1)
        memory=await services.memory.context(user,old['id'])
        checks['flash_summary']=bool(memory['summary']) and not memory['summary_pending']
        checks['rich_profile']=memory['profile'].get('os')=='Windows 11' and 'IntelliJ' in memory['profile'].get('ide','') and bool(memory['profile'].get('preferences'))
        checks['segmented_archive']=services.memory.inspect(user)['episodes']==4
        checks['raw_messages_preserved']=store.history(user,old['id'])==original
        checks['real_private_chroma']=memory['backend']=='chroma-private' and bool(memory['relevant_history']) and not memory['degraded']
        checks['real_redis_hit']=(await services.memory.context(user,old['id']))['cache_hit']
        checks['other_user_isolated']=services.memory.inspect(Principal('demo','other'))['episodes']==0
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://verify',headers={'Authorization':'Bearer '+token},timeout=120) as http:
            result=await http.post('/runs',json={'message':'VPN仍然连接超时，请按你记得的环境和我偏好的讲解方式给排查建议，不需要提交工单。','request_key':'verify-current-'+identity})
            result.raise_for_status();run=result.json()
            async with asyncio.timeout(120):
                while True:
                    final=store.get_run(user,run['id'])
                    if final['status'] not in {'queued','running'}:break
                    await asyncio.sleep(.1)
            checkpoint=store.load_checkpoint(user,run['id']) or {}
            injected=checkpoint.get('context',{}).get('memory',{})
            checks['current_agent_injects_profile']=injected.get('profile',{}).get('os')=='Windows 11'
            checks['current_agent_cross_run_recall']=any(item['run_id']==old['id'] for item in injected.get('relevant_history',[]))
            checks['current_agent_finishes']=final['status']=='completed' and not final.get('error')
            checks['no_business_write']=not store.list_tickets(user)
            api=await http.get('/memory');api.raise_for_status()
            checks['authenticated_memory_api']=api.json()['backend']=='chroma-private'
    finally:
        await agent.close()
        after=model.ledger().summary()
        report={'kind':'current_asgi_real_memory','simulation':True,'checks':checks,'passed':sum(checks.values()),'total':len(checks),
            'budget_before':before,'budget_after':after,'dependencies':'real PostgreSQL Redis Chroma local E5 stdio MCP; only deepseek-flash is paid'}
        evidence=ROOT/'reports'/('memory-runtime-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
        with evidence.open('x',encoding='utf-8') as dest:json.dump(report,dest,ensure_ascii=False,indent=2)
        print(json.dumps({'passed':report['passed'],'total':report['total'],'evidence':str(evidence.relative_to(ROOT))}),flush=True)
        from tools.migrate_services import TABLES
        with store.database.engine.begin() as db:
            tables=set(db.execute(text('SELECT tablename FROM pg_tables WHERE schemaname=:s'),{'s':schema}).scalars())
            if not schema.startswith('verify_memory_') or not tables<=TABLES:raise RuntimeError('Unexpected synthetic scope; preserving')
            db.exec_driver_sql('DROP SCHEMA "'+schema+'" CASCADE')
        if memory_name:
            expected='resolvedesk-memory-'+hashlib.sha256(collection.encode()).hexdigest()[:16]
            if memory_name!=expected:raise RuntimeError('Unexpected private fixture collection')
            vectors.request('DELETE',vectors.base+'/'+memory_name)
        if vectors.name!=collection or not collection.startswith('verify-memory-'):raise RuntimeError('Unexpected fixture collection')
        vectors.request('DELETE',vectors.base+'/'+collection);vectors.close();store.close()
        keys=[key async for key in cache.redis.scan_iter(match=cache.namespace+':*')]
        if any(not key.startswith(cache.namespace+':') for key in keys):raise RuntimeError('Unexpected fixture keys')
        if keys:await cache.redis.delete(*keys)
        await cache.close();await model.close()
    if not checks or not all(checks.values()):raise SystemExit(1)


if __name__=='__main__':asyncio.run(main())
