"""Free standard-SDK MCP checks against an already deployed local gateway/host."""
import asyncio
from datetime import datetime,timezone
import json
import logging
from pathlib import Path
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT=Path(__file__).resolve().parents[1]


async def main():
    logging.disable(logging.INFO)
    base=sys.argv[1] if len(sys.argv)>1 else 'http://127.0.0.1:18080'
    if base not in {'http://127.0.0.1:8080','http://127.0.0.1:18080'}:raise ValueError('Local checks only')
    access=json.loads((ROOT/'.codex-runtime/access.json').read_text(encoding='utf-8'))
    checks={};details={}
    async with httpx.AsyncClient(base_url=base,timeout=20,trust_env=False) as api:
        async def request(method,path,body=None,actor='support'):
            result=await api.request(method,path,json=body,headers={'Authorization':'Bearer '+access[actor]['token']})
            result.raise_for_status();return result.json()
        before=(await request('GET','/system'))['model']
        checks['unauthenticated_rejected']=(await api.post('/mcp/knowledge/',json={})).status_code==401
        status=await request('GET','/integrations/mcp')
        checks['protocol_is_distinct_from_status']=status['protocol_endpoint']=='/mcp/knowledge/' and all(s['state']=='connected' for s in status['servers'])
        async with httpx.AsyncClient(timeout=20,trust_env=False,headers={'Authorization':'Bearer '+access['learner']['token']}) as http:
            async with streamable_http_client(base+'/mcp/knowledge/',http_client=http) as (read,write,_):
                async with ClientSession(read,write) as session:
                    initialized=await session.initialize()
                    details['protocol_version']=initialized.protocolVersion
                    catalog=await session.list_tools()
                    checks['standard_tool_discovery']=[t.name for t in catalog.tools]==['search_local_knowledge'] and catalog.tools[0].annotations.readOnlyHint
                    result=await session.call_tool('search_local_knowledge',{'query':'VPN','top_k':2})
                    checks['current_knowledge_read']=not result.isError and bool(result.structuredContent['results']) and not result.structuredContent['degraded']
                    stats=await session.read_resource('resolvedesk://knowledge/stats')
                    details['visible_chunks']=json.loads(stats.contents[0].text)['total_chunks']
                    checks['stats_resource']=details['visible_chunks']>0
                    source=result.structuredContent['results'][0]
                    resource=await session.read_resource('resolvedesk://knowledge/citations/'+source['citation'])
                    authoritative=await request('GET','/knowledge/citations/'+source['citation'],actor='learner')
                    checks['citation_matches_current_api']=json.loads(resource.contents[0].text)==authoritative
                    templates=await session.list_resource_templates()
                    checks['citation_template_discovery']=any('citations/{citation}' in str(t.uriTemplate) for t in templates.resourceTemplates)
        body={'tool':'mcp_support_support_checklist','arguments':{'category':'network'},'role':'technical'}
        denied=await api.post('/integrations/mcp/call',json=body,headers={'Authorization':'Bearer '+access['learner']['token']})
        checks['debug_support_only']=denied.status_code==403
        debug=await request('POST','/integrations/mcp/call',body)
        checks['real_stdio_debug']=debug['structured']['category']=='network' and debug['mcp_server']=='support'
        intent=await request('GET','/integrations/intent-embedding')
        checks['current_intent_provider_status']=intent['configured'] and bool(intent['provider']) and intent['state'] in {'cold','ready','cooldown'}
        after=(await request('GET','/system'))['model']
        checks['model_configuration_unchanged']=before==after
        report={'base':base,'checks':checks,'details':details,'passed':sum(checks.values()),'total':len(checks),'model_configuration':after,'model_configuration_changed':False}
    path=ROOT/'reports'/('mcp-deployment-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    with path.open('x',encoding='utf-8') as dest:json.dump(report,dest,ensure_ascii=False,indent=2)
    print(json.dumps({'passed':report['passed'],'total':report['total'],'evidence':str(path.relative_to(ROOT))}),flush=True)
    if not all(checks.values()):raise SystemExit(1)


if __name__=='__main__':asyncio.run(main())
