"""G07 current MCP protocol + configured HTTP/stdio and actual Flash Agent.

Uses fresh synthetic PG/Chroma/Redis scopes and a dedicated local HTTP fixture.
Only the original cumulative financial authority is shared. No business writes.
"""
import argparse
import asyncio
from datetime import datetime,timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true')
    if not parser.parse_args().live:parser.error('Use --live for the authorized real Flash Agent verification')
    logging.disable(logging.INFO)
    from dotenv import load_dotenv
    import httpx
    from sqlalchemy import text
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from core.paid_api import BudgetedAnthropic
    from resolvedesk.settings import ServiceSettings
    from resolvedesk.store import Store,Principal
    from resolvedesk.retrieval import LocalEmbedding,HybridKnowledge
    from resolvedesk.vectors import ChromaVectors
    from resolvedesk.coordination import Coordination
    from resolvedesk.services import DeskServices
    from resolvedesk.agent import DeskAgent
    from resolvedesk.app import create_app
    from tools.migrate_services import validate_path,TABLES
    sys.path.insert(0,str(ROOT/'tests'))
    from mcp_http_fixture import HTTPFixture
    load_dotenv(ROOT/'.env',override=False)
    identity=uuid.uuid4().hex;schema='verify_mcp_'+identity;collection='verify-mcp-'+identity
    directory=validate_path(ROOT/'.codex-tmp'/('mcp-live-'+identity));directory.mkdir(exist_ok=False)
    settings=ServiceSettings.load();store=Store(settings.database_url,schema=schema)
    embedding=LocalEmbedding(ROOT/'data/models/multilingual-e5-small')
    vectors=ChromaVectors(settings.chroma_url,embedding.identity,collection=collection)
    cache=Coordination(settings.redis_url,'verify-mcp-'+identity)
    kb=HybridKnowledge(store,embedding,vectors)
    user=Principal('demo','learner');support=Principal('demo','support','support');other=Principal('demo','observer','support')
    kb.add(support,'vpn-support','VPN支持制度','# VPN排障\n\n【合成模拟制度】VPN连接超时时先核对普通网络、记录客户端版本和错误码，不要发送密码，必要时通过现有IT渠道联系人员。')
    kb.add(other,'observer-private','私有测试资料','VPN 私有合成内容，仅该用户可见。',visibility='private')
    fixture=HTTPFixture(secrets.token_urlsafe(32));await fixture.start()
    os.environ['MCP_VERIFICATION_AUTH']='Bearer '+fixture.token
    configuration=json.loads((ROOT/'deploy/mcp_servers.json').read_text(encoding='utf-8'))
    configuration['servers'].append({'name':'remote','transport':'streamable_http','url':fixture.url,
        'headers_env':{'Authorization':'MCP_VERIFICATION_AUTH'},'allowed_tools':['external_check','dangerous_write'],
        'agents':['technical'],'timeout_s':2,'cache_ttl':0,'ping_interval_s':.5,'read_only':True})
    config=directory/'servers.json'
    with config.open('x',encoding='utf-8') as dest:json.dump(configuration,dest,ensure_ascii=False,indent=2)
    os.environ['RESOLVEDESK_MCP_SERVERS_CONFIG']=str(config)
    client=BudgetedAnthropic(api_key=os.getenv('ANTHROPIC_API_KEY',''),base_url='https://api.deepseek.com/anthropic',timeout=25)
    before=client.ledger().summary()
    services=DeskServices(store,kb,client,coordination=cache);agent=DeskAgent(store,kb,client,services,cache)
    tokens={p.user:secrets.token_urlsafe(36) for p in (user,support)}
    accounts={hashlib.sha256(tokens[p.user].encode()).hexdigest():vars(p) for p in (user,support)}
    app=create_app(store,kb,agent,accounts);checks={};details={};memory_name=None
    try:
        await services.start();memory_name=services.memory_vectors.name
        async with app.router.lifespan_context(app):
            state=services.mcp.summary()['servers']
            checks['normal_config_two_transports']={r['transport'] for r in state}=={'stdio','streamable_http'} and all(r['state']=='connected' for r in state)
            alias='mcp_remote_external_check'
            checks['readonly_role_whitelist']=alias in services.external_tools('technical') and alias not in services.external_tools('general') and 'mcp_remote_dangerous_write' not in services.external_tools('technical')
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),headers={'Authorization':'Bearer '+tokens[user.user]}) as http:
                async with streamable_http_client('http://localhost/mcp/knowledge/',http_client=http) as (read,write,_):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        tools=await session.list_tools()
                        checks['standard_current_protocol']=tools.tools[0].name=='search_local_knowledge' and tools.tools[0].annotations.readOnlyHint
                        result=await session.call_tool('search_local_knowledge',{'query':'VPN'})
                        checks['real_knowledge_acl']=not result.isError and {r['document_id'] for r in result.structuredContent['results']}=={'vpn-support'}
                        stats=await session.read_resource('resolvedesk://knowledge/stats')
                        checks['scoped_stats_resource']=json.loads(stats.contents[0].text)['total_chunks']==1
                        citation=result.structuredContent['results'][0]['citation']
                        original=await session.read_resource('resolvedesk://knowledge/citations/'+citation)
                        checks['citation_resource']=json.loads(original.contents[0].text)['document_id']=='vpn-support'
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://localhost') as http:
                checks['protocol_requires_login']=(await http.post('/mcp/knowledge/',json={})).status_code==401
                body={'tool':alias,'arguments':{'category':'network'},'role':'technical'}
                blocked=await http.post('/integrations/mcp/call',json=body,headers={'Authorization':'Bearer '+tokens[user.user]})
                checks['debug_support_only']=blocked.status_code==403
                debug=await http.post('/integrations/mcp/call',json=body,headers={'Authorization':'Bearer '+tokens[support.user]});debug.raise_for_status()
                checks['debug_redacts_credentials']=fixture.token not in debug.text and debug.json()['mcp_server']=='remote'
                status=await http.get('/integrations/intent-embedding',headers={'Authorization':'Bearer '+tokens[support.user]})
                checks['intent_status_current_provider']=status.json()['configured'] and status.json()['provider']==embedding.identity and status.json()['state']=='cold'
            version=services.manager._cache_versions[alias]
            await services.mcp.connections['remote'].session.call_tool('refresh_catalog',{})
            async with asyncio.timeout(5):
                while services.manager._cache_versions[alias]<=version:await asyncio.sleep(.02)
            checks['http_catalog_refresh']=services.manager._cache_versions[alias]>version
            count=services.mcp.connections['remote'].connections
            await fixture.stop();await fixture.start()
            async with asyncio.timeout(15):
                while services.mcp.connections['remote'].connections<=count or services.mcp.connections['remote'].state!='connected':await asyncio.sleep(.05)
            checks['http_passive_reconnect']=services.mcp.connections['remote'].connections>count
            checks['free_protocol_and_debug_no_spend']=client.ledger().summary()==before
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://localhost',headers={'Authorization':'Bearer '+tokens[user.user]}) as http:
                result=await http.post('/runs',json={'message':'VPN连接超时，请先调用 mcp_remote_external_check，category填network，验证外部只读清单，再依据制度给出建议。不要建单。','request_key':'verify-mcp-'+identity})
                result.raise_for_status();run=result.json()
                async with asyncio.timeout(120):
                    while True:
                        final=store.get_run(user,run['id'])
                        if final['status'] not in {'queued','running'}:break
                        await asyncio.sleep(.1)
            events=store.events(user,run['id'])
            traces=[trace for e in events if e['kind']=='specialist_result' for trace in e['payload'].get('tool_traces',[])]
            used=[trace for trace in traces if trace.get('tool_name')==alias]
            checks['flash_professional_calls_configured_http']=bool(used) and any(t.get('mcp_server')=='remote' and t.get('source')=='external_mcp' and t.get('success') for t in used)
            checks['current_agent_completed']=final['status']=='completed' and '[C-' in final['answer'] and not final.get('error')
            checks['no_business_write']=not store.list_tickets(user)
            checks['transport_credential_not_persisted']=fixture.token not in json.dumps(events,ensure_ascii=False)
            await services.memory.remember(user,run['id'])
            async with asyncio.timeout(60):
                while services.memory.inspect(user)['maintenance']['state']!='idle':await asyncio.sleep(.1)
            details={'final_status':final['status'],'http_connections':services.mcp.connections['remote'].connections,
                'professional_tools':[{k:t.get(k) for k in ('tool_name','source','mcp_server','mcp_tool','success','cached')} for t in used]}
    finally:
        await agent.close();await fixture.stop()
        after=client.ledger().summary()
        report={'kind':'current_asgi_real_mcp','simulation':True,'checks':checks,'details':details,'passed':sum(checks.values()),'total':len(checks),
            'budget_before':before,'budget_after':after,'source_revision':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()}
        evidence=ROOT/'reports'/('mcp-runtime-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
        with evidence.open('x',encoding='utf-8') as dest:json.dump(report,dest,ensure_ascii=False,indent=2)
        print(json.dumps({'passed':report['passed'],'total':report['total'],'evidence':str(evidence.relative_to(ROOT))}),flush=True)
        with store.database.engine.begin() as db:
            tables=set(db.execute(text('SELECT tablename FROM pg_tables WHERE schemaname=:s'),{'s':schema}).scalars())
            if not schema.startswith('verify_mcp_') or not tables<=TABLES:raise RuntimeError('Unexpected synthetic schema')
            db.exec_driver_sql('DROP SCHEMA "'+schema+'" CASCADE')
        if memory_name:
            if memory_name!='resolvedesk-memory-'+hashlib.sha256(collection.encode()).hexdigest()[:16]:raise RuntimeError('Unexpected memory scope')
            vectors.request('DELETE',vectors.base+'/'+memory_name)
        if vectors.name!=collection or not collection.startswith('verify-mcp-'):raise RuntimeError('Unexpected vector scope')
        vectors.request('DELETE',vectors.base+'/'+collection);vectors.close();store.close()
        keys=[key async for key in cache.redis.scan_iter(match=cache.namespace+':*')]
        if any(not key.startswith(cache.namespace+':') for key in keys):raise RuntimeError('Unexpected fixture keys')
        if keys:await cache.redis.delete(*keys)
        await cache.close();await client.close()
        if set(directory.iterdir())!={config} or config.is_symlink() or config.is_junction():raise RuntimeError('Unexpected fixture configuration files')
        config.unlink();directory.rmdir()
    if not checks or not all(checks.values()):raise SystemExit(1)


if __name__=='__main__':asyncio.run(main())
