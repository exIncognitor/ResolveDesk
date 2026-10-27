"""G07 current auth, standard Resources, configured transports and reconnect."""
import asyncio
import hashlib
import json
from pathlib import Path
import secrets

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from resolvedesk.agent import DeskAgent
from resolvedesk.app import create_app
from resolvedesk.intent import ITIntentRecognizer
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.services import DeskServices
from resolvedesk.store import Principal,Store
from tooling.mcp_client import load_servers
from test_resolvedesk import USER,OTHER,FOREIGN,SUPPORT,FakeEmbedding,client_with
from mcp_http_fixture import HTTPFixture


@pytest.fixture
def environment(tmp_path):
    store=Store(tmp_path/'mcp.sqlite3');kb=HybridKnowledge(store,FakeEmbedding());model=client_with()
    kb.add(SUPPORT,'shared','VPN公开','VPN 公开排查')
    kb.add(Principal('demo','alice','support'),'alice-private','VPN Alice','VPN Alice 私有',visibility='private')
    kb.add(Principal('demo','bob','support'),'bob-private','VPN Bob','VPN Bob 私有',visibility='private')
    kb.add(FOREIGN,'foreign','VPN他租户','VPN 他租户')
    services=DeskServices(store,kb,model,mcp_configs=[]);agent=DeskAgent(store,kb,model,services)
    accounts={hashlib.sha256(token.encode()).hexdigest():vars(p) for token,p in [('alice',USER),('bob',OTHER),('foreign',FOREIGN),('support',SUPPORT)]}
    app=create_app(store,kb,agent,accounts)
    yield store,kb,model,services,app
    store.close()


@pytest.mark.asyncio
async def test_current_protocol_auth_resources_acl_token_switch_and_no_paid_calls(environment):
    store,kb,model,services,app=environment
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://localhost') as raw:
            assert (await raw.post('/mcp/knowledge/',json={})).status_code==401
            assert (await raw.get('/mcp')).status_code==401
            assert (await raw.get('/mcp',headers={'Authorization':'Bearer alice'})).json()=={'servers':[]}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),headers={'Authorization':'Bearer alice'}) as http:
            async with streamable_http_client('http://localhost/mcp/knowledge/',http_client=http) as (read,write,_):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    catalog=await session.list_tools()
                    assert [tool.name for tool in catalog.tools]==['search_local_knowledge']
                    assert catalog.tools[0].annotations.readOnlyHint
                    result=await session.call_tool('search_local_knowledge',{'query':'VPN','mode':'bm25'})
                    assert not result.isError and {i['document_id'] for i in result.structuredContent['results']}=={'shared','alice-private'}
                    stats=await session.read_resource('resolvedesk://knowledge/stats')
                    assert json.loads(stats.contents[0].text)['total_chunks']==2
                    templates=await session.list_resource_templates()
                    assert any('citations/{citation}' in str(t.uriTemplate) for t in templates.resourceTemplates)
                    private=next(i for i in result.structuredContent['results'] if i['document_id']=='alice-private')
                    content=await session.read_resource('resolvedesk://knowledge/citations/'+private['citation'])
                    assert json.loads(content.contents[0].text)['document_id']=='alice-private'
                    kb.add(Principal('demo','alice','support'),'alice-private','新版','VPN Alice 新版',visibility='private')
                    with pytest.raises(McpError):await session.read_resource('resolvedesk://knowledge/citations/'+private['citation'])
                    http.headers['Authorization']='Bearer bob'
                    switched=await session.call_tool('search_local_knowledge',{'query':'VPN','tenant':'other','user':'alice'})
                    assert switched.isError or {i['document_id'] for i in switched.structuredContent['results']}=={'shared','bob-private'}
                    with pytest.raises(McpError):await session.read_resource('resolvedesk://knowledge/citations/'+private['citation'])
                    http.headers['Authorization']='Bearer foreign'
                    foreign=await session.call_tool('search_local_knowledge',{'query':'VPN'})
                    assert {i['document_id'] for i in foreign.structuredContent['results']}=={'foreign'}
                    invalid=await session.call_tool('search_local_knowledge',{'query':'VPN','top_k':0})
                    assert invalid.isError
        model.messages.create.assert_not_called()


@pytest.mark.asyncio
async def test_current_protocol_concurrent_identities_and_replaced_citation(environment):
    store,kb,model,services,app=environment
    async def read_as(token):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),headers={'Authorization':'Bearer '+token}) as http:
            async with streamable_http_client('http://localhost/mcp/knowledge/',http_client=http) as (read,write,_):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    result=await session.call_tool('search_local_knowledge',{'query':'VPN'})
                    return {i['document_id'] for i in result.structuredContent['results']}
    async with app.router.lifespan_context(app):
        results=await asyncio.gather(*(read_as(token) for token in ['alice','bob','foreign']*3))
        assert results==[{'shared','alice-private'},{'shared','bob-private'},{'foreign'}]*3
    model.messages.create.assert_not_called()


@pytest.mark.asyncio
async def test_normal_config_http_auth_professional_wire_refresh_restart_and_debug(environment,tmp_path,monkeypatch):
    store,kb,model,unused,app=environment
    fixture=HTTPFixture(secrets.token_urlsafe(24));await fixture.start()
    monkeypatch.setenv('MCP_TEST_HTTP_AUTH','Bearer '+fixture.token)
    path=tmp_path/'servers.json'
    path.write_text(json.dumps({'servers':[{'name':'remote','transport':'streamable_http','url':fixture.url,
        'headers_env':{'Authorization':'MCP_TEST_HTTP_AUTH'},'allowed_tools':['external_check','dangerous_write'],
        'agents':['technical'],'read_only':True,'timeout_s':1,'cache_ttl':0,'ping_interval_s':.2}]}),encoding='utf-8')
    monkeypatch.setenv('RESOLVEDESK_MCP_SERVERS_CONFIG',str(path))
    services=DeskServices(store,kb,model)
    app.state.agent.services=services
    try:
        await services.start()
        state=services.mcp.summary()['servers'][0]
        assert state['transport']=='streamable_http' and state['state']=='connected'
        alias='mcp_remote_external_check'
        assert set(services.external_tools('technical'))=={alias} and not services.external_tools('general')
        result=await services.call_external(USER,alias,{'category':'network'},'technical')
        assert result['structured']['category']=='network' and fixture.token not in json.dumps(result)
        monkeypatch.setenv('MCP_TEST_HTTP_AUTH','Bearer replacement-synthetic-credential')
        rotated=await services.call_external(USER,alias,{'category':'network'},'technical')
        assert fixture.token not in json.dumps(rotated)
        monkeypatch.setenv('MCP_TEST_HTTP_AUTH','Bearer '+fixture.token)
        with pytest.raises(PermissionError):await services.call_external(USER,alias,{'category':'network'},'general')
        from agents.agent_orchestrator import AgentType
        assert alias in services.roles._pool[AgentType.TECHNICAL][0].get_tools()
        version=services.manager._cache_versions[alias]
        await services.mcp.connections['remote'].session.call_tool('refresh_catalog',{})
        async with asyncio.timeout(4):
            while services.manager._cache_versions[alias]<=version:await asyncio.sleep(.02)
        connection_count=services.mcp.connections['remote'].connections
        await fixture.stop();await fixture.start()
        async with asyncio.timeout(10):
            while services.mcp.connections['remote'].connections<=connection_count or services.mcp.connections['remote'].state!='connected':await asyncio.sleep(.05)
        recovered=await services.call_external(USER,alias,{'category':'hardware'},'technical')
        assert recovered['structured']['category']=='hardware'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://localhost') as http:
            data={'tool':alias,'arguments':{'category':'network'},'role':'technical'}
            assert (await http.post('/integrations/mcp/call',json=data)).status_code==401
            assert (await http.post('/integrations/mcp/call',json=data,headers={'Authorization':'Bearer alice'})).status_code==403
            allowed=await http.post('/integrations/mcp/call',json=data,headers={'Authorization':'Bearer support'})
            assert allowed.status_code==200 and fixture.token not in allowed.text
            denied=await http.post('/integrations/mcp/call',json={**data,'tool':'mcp_remote_dangerous_write'},headers={'Authorization':'Bearer support'})
            assert denied.status_code==403
        model.messages.create.assert_not_called()
    finally:
        await services.close();await fixture.stop();await unused.close()


def test_config_macros_strict_flags_missing_headers_and_no_model_key_forwarding(tmp_path):
    path=tmp_path/'config.json'
    path.write_text(json.dumps({'servers':[{'name':'local','transport':'stdio','command':'{python}',
        'args':['{project_root}/resolvedesk/mcp_support.py'],'allowed_tools':['support_checklist'],'agents':['general']}]}),encoding='utf-8')
    config=load_servers(path)[0]
    assert Path(config.command).is_file() and Path(config.args[0]).is_file()
    from tooling.mcp_client import MCPServerConfig
    base=dict(name='bad',transport='streamable_http',url='https://example.com/mcp',allowed_tools=['read'],agents=['general'])
    for changes in [{'read_only':'false'},{'url':'https://example.com/mcp?token=not-allowed'},
        {'headers_env':{'Authorization':'ANTHROPIC_API_KEY'}},{'env_vars':['DEEPSEEK_API_KEY']},{'headers_env':{'Authorization':'a value rather than a variable'}}]:
        with pytest.raises(ValueError):MCPServerConfig(**(base|changes))
    for changes in [{'allowed_tools':'read'},{'headers_env':{'Authorization':'anthropic_api_key'}},{'timeout_s':True},{'max_result_chars':300.5}]:
        with pytest.raises(ValueError):MCPServerConfig(**(base|changes))


@pytest.mark.asyncio
async def test_intent_embedding_status_is_real_cooldown_and_endpoint_is_free(environment):
    store,kb,model,services,app=environment
    class Flaky:
        identity='test-vector-status'
        failed=True
        def embed(self,texts,query=False):
            if self.failed:raise ConnectionError('synthetic error')
            return [[1.,0.] for _ in texts]
    embedding=Flaky();recognizer=ITIntentRecognizer(model,embedding)
    services.roles._intent_recognizer=recognizer
    assert recognizer.embedding_status()['state']=='cold'
    assert not (await recognizer.vectors('VPN'))['available']
    assert recognizer.embedding_status()['state']=='cooldown'
    await recognizer.vectors('VPN again');assert recognizer.vector_attempts==1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://localhost') as http:
        assert (await http.get('/integrations/intent-embedding',headers={'Authorization':'Bearer alice'})).status_code==403
        result=await http.get('/integrations/intent-embedding',headers={'Authorization':'Bearer support'})
        assert result.json()['last_error']=='ConnectionError' and result.json()['cooldown_remaining_s']>0
    embedding.failed=False;recognizer.vector_retry_at=0
    assert (await recognizer.vectors('VPN'))['available'] and recognizer.embedding_status()['state']=='ready'
    model.messages.create.assert_not_called();await services.close()


@pytest.mark.asyncio
async def test_missing_transport_credentials_and_write_config_do_not_execute_tools(environment,tmp_path,monkeypatch):
    store,kb,model,unused,app=environment
    from tooling.mcp_client import MCPServerConfig
    write=MCPServerConfig(name='write',transport='stdio',command='must-not-execute',allowed_tools=['write'],agents=['general'],read_only=False)
    with pytest.raises(ValueError):DeskServices(store,kb,model,mcp_configs=[write])
    monkeypatch.delenv('MCP_UNCONFIGURED_TEST_AUTH',raising=False)
    config=MCPServerConfig(name='missing',transport='streamable_http',url='http://127.0.0.1:1/mcp',
        headers_env={'Authorization':'MCP_UNCONFIGURED_TEST_AUTH'},allowed_tools=['read'],agents=['general'],timeout_s=1)
    service=DeskServices(store,kb,model,mcp_configs=[config])
    try:
        await service.start()
        state=service.mcp.summary()['servers'][0]
        assert state['state']=='disconnected' and state['error']=='ValueError' and state['connections']==0
        assert not service.external_tools('general')
        model.messages.create.assert_not_called()
    finally:await service.close();await unused.close()
