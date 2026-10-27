"""G11 bounded import, atomic SQL, and interactive current-service API behavior."""
import asyncio
import hashlib
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import httpx
import pytest

from resolvedesk.agent import DeskAgent
from resolvedesk.app import create_app
from resolvedesk.cli import DeskCLI,base_url
from resolvedesk.imports import MAX_IMPORT_BYTES
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.store import Principal,Store
from test_resolvedesk import USER,SUPPORT,OTHER,client_with,response
from test_database_backends import postgres_store
from test_chroma_projection import vectors
from test_redis_coordination import cache


@pytest.fixture
def api(tmp_path):
    store=Store(tmp_path/'imports.sqlite3');kb=HybridKnowledge(store);agent=DeskAgent(store,kb,client_with())
    accounts={hashlib.sha256(name.encode()).hexdigest():vars(p) for name,p in (('support-api',SUPPORT),('user-api',USER))}
    app=create_app(store,kb,agent,accounts)
    with TestClient(app) as client:
        client.headers['Authorization']='Bearer support-api'
        yield store,kb,app,client
    store.close()


def test_batch_and_upload_are_real_server_imports_and_enforce_identity(api):
    store,kb,app,client=api
    body={'documents':[{'title':'VPN制度','content':'# VPN\n\n检查普通网络。'},{'title':'账号制度','content':'# 账号\n\n通过现有IT渠道核验。','visibility':'private'}]}
    first=client.post('/knowledge/add',json=body);assert first.status_code==200
    assert first.json()['documents']==2 and first.json()['atomic_sql']
    second=client.post('/knowledge/batch',json=body).json()
    assert [r['document_id'] for r in first.json()['results']]==[r['document_id'] for r in second['results']]
    with store.db() as db:assert db.execute('SELECT COUNT(*) FROM knowledge_documents').fetchone()[0]==2
    uploaded=client.post('/knowledge/upload',files={'file':('diagnostics.MD',b'# Network\n\n' + b'a'*300001,'text/markdown')})
    assert uploaded.status_code==200 and uploaded.json()['added_chunks']>400
    assert client.post('/knowledge/upload',files={'file':('docs.json',json.dumps(body['documents'],ensure_ascii=False).encode(),'application/json')}).status_code==200
    client.headers['Authorization']='Bearer user-api'
    assert client.post('/knowledge/add',json=body).status_code==403
    assert client.post('/knowledge/upload',files={'file':('a.md',b'data')}).status_code==403


def test_upload_boundary_is_ten_mib_without_browser_parsing(api,monkeypatch):
    _,kb,_,client=api;received=[]
    # Parser boundary test; large embedding/index cost is deliberately omitted.
    def capture(p,docs):received.append(len(docs[0]['content'].encode()));return {'documents':1}
    monkeypatch.setattr(kb,'add_many',capture)
    assert client.post('/knowledge/upload',files={'file':('limit.md',b'x'*MAX_IMPORT_BYTES)}).status_code==200
    assert received==[MAX_IMPORT_BYTES]
    assert client.post('/knowledge/upload',files={'file':('too-large.md',b'x'*(MAX_IMPORT_BYTES+1))}).status_code==413
    assert received==[MAX_IMPORT_BYTES]


@pytest.mark.parametrize('payload',[
    {'documents':[]},{'documents':[{'title':'a','content':'x','owner':'injected'}]},
    {'documents':[{'title':'a','content':'x','document_id':'same'},{'title':'b','content':'y','document_id':'same'}]},
    {'documents':[{'title':' ','content':'x'}]},
    {'documents':[{'title':'a','content':'x'}]*101},
])
def test_batch_rejects_invalid_whole_input_before_any_write(api,payload):
    store,_,_,client=api
    assert client.post('/knowledge/add',json=payload).status_code==422
    with store.db() as db:assert db.execute('SELECT COUNT(*) FROM knowledge_documents').fetchone()[0]==0


def test_file_format_utf8_and_outer_json_limits(api):
    _,_,_,client=api
    assert client.post('/knowledge/upload',files={'file':('a.exe',b'x')}).status_code==415
    assert client.post('/knowledge/upload',files={'file':('a.md',b'\xff')}).status_code==422
    assert client.post('/knowledge/upload',files={'file':('a.json',b'{}')}).status_code==422
    assert client.post('/knowledge/upload',files={'file':('a.json',b'[')}).status_code==422
    assert client.post('/knowledge/add',content=b'x'*(MAX_IMPORT_BYTES+1),headers={'Content-Type':'application/json'}).status_code==413


@pytest.mark.asyncio
async def test_cancelled_http_import_keeps_bounded_thread_slots_until_actual_completion(tmp_path,monkeypatch):
    store=Store(tmp_path/'bounded-import.sqlite3');kb=HybridKnowledge(store);agent=DeskAgent(store,kb,client_with())
    account={hashlib.sha256(b'bounded-token').hexdigest():vars(SUPPORT)}
    app=create_app(store,kb,agent,account);gate=threading.Event();entered=0;lock=threading.Lock()
    original=kb.add_many
    def slow(p,docs):
        nonlocal entered
        with lock:entered+=1
        if not gate.wait(5):raise RuntimeError('Fixture gate timeout')
        return original(p,docs)
    monkeypatch.setattr(kb,'add_many',slow)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://localhost',headers={'Authorization':'Bearer bounded-token'}) as http:
        tasks=[asyncio.create_task(http.post('/knowledge/add',json={'documents':[{'title':str(i),'content':'test'}]})) for i in range(2)]
        try:
            async with asyncio.timeout(3):
                while entered<2:await asyncio.sleep(.005)
            tasks[0].cancel();await asyncio.gather(tasks[0],return_exceptions=True)
            response=await http.post('/knowledge/documents',json={'document_id':'third','title':'third','content':'data'})
            assert response.status_code==429 and entered==2
        finally:
            gate.set();await asyncio.gather(*tasks,return_exceptions=True)
        async with asyncio.timeout(3):
            while app.state.import_tasks:await asyncio.sleep(.005)
        assert app.state.import_slots._value==2
    store.close()


@pytest.mark.parametrize('backend',['sqlite','postgres'])
def test_batch_rolls_back_all_documents_on_private_ownership_conflict(api,backend,request):
    store=api[0] if backend=='sqlite' else request.getfixturevalue('postgres_store');kb=HybridKnowledge(store)
    kb.add(SUPPORT,'a-public','public','original')
    kb.add(Principal('demo','private-owner','support'),'z-private','private','private-original','private')
    with pytest.raises(PermissionError):kb.add_many(SUPPORT,[{'document_id':'a-public','title':'changed','content':'changed','visibility':'tenant'},
        {'document_id':'z-private','title':'overwrite','content':'bad','visibility':'tenant'}])
    with store.db() as db:
        assert db.execute("SELECT content FROM chunks WHERE document_id='a-public'").fetchone()[0]=='original'
        assert db.execute("SELECT content FROM chunks WHERE document_id='z-private'").fetchone()[0]=='private-original'


def test_embedding_failure_leaves_whole_batch_unwritten(api):
    store,_,_,_=api
    class BrokenEmbedding:
        identity='explicit-broken-fixture'
        def embed(self,texts):
            if any('fail' in t for t in texts):raise RuntimeError('synthetic embedding failure')
            return [[1,0] for _ in texts]
    kb=HybridKnowledge(store,BrokenEmbedding())
    with pytest.raises(RuntimeError):kb.add_many(SUPPORT,[{'document_id':'one','title':'one','content':'valid','visibility':'tenant'},{'document_id':'two','title':'two','content':'fail','visibility':'tenant'}])
    with store.db() as db:assert db.execute('SELECT COUNT(*) FROM knowledge_documents').fetchone()[0]==0


def test_cli_ambiguous_reply_reuses_key_and_same_saved_turn_without_printing_token():
    posts=[];reads=[];output=[];lost=True
    def transport(req):
        nonlocal lost
        if req.method=='POST':
            body=json.loads(req.content);posts.append((str(req.url),body))
            if lost:lost=False;raise httpx.ReadError('synthetic lost response',request=req)
            return httpx.Response(200,json={'id':'r','status':'completed','turn_key':body['request_key'],'answer':'saved answer','latency_ms':50})
        reads.append(req.url.path);return httpx.Response(200,json={'id':'r','status':'completed'})
    with httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(transport),headers={'Authorization':'Bearer private-api-token'}) as h:
        cli=DeskCLI(h,write=output.append);cli.run_id='r'
        with pytest.raises(httpx.ReadError):cli.chat('same question')
        with pytest.raises(ValueError):cli.chat('new question')
        cli.command(':retry')
    assert posts[0]==posts[1] and posts[0][0].endswith('/reply')
    assert any('saved answer' in item for item in output)
    assert 'private-api-token' not in ''.join(output)


def test_cli_resume_uncertain_response_is_observed_without_replaying_execution():
    posts=[];out=[]
    def transport(req):
        if req.method=='POST':posts.append(req.url.path);raise httpx.ReadError('lost resume response',request=req)
        return httpx.Response(200,json={'id':'r','status':'failed','turn_key':'same-key','answer':'preserved failure'})
    with httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(transport)) as h:
        cli=DeskCLI(h,write=out.append);cli.run_id='r'
        with pytest.raises(httpx.ReadError):cli.command(':resume')
        cli.command(':retry')
    assert posts==['/runs/r/resume']


def test_cli_draft_confirmation_requires_explicit_matching_version():
    posts=[];out=[]
    def transport(req):
        if req.method=='POST':posts.append(json.loads(req.content));return httpx.Response(200,json={'id':'ticket','simulation':True})
        return httpx.Response(200,json={'id':'r','status':'awaiting_confirmation','version':3,'approval':'private-approval','draft':{'summary':'request'}})
    with httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(transport)) as h:
        cli=DeskCLI(h,write=out.append);cli.run_id='r'
        for text in (':confirm',':confirm 2'):
            with pytest.raises(ValueError):cli.command(text)
        assert not posts
        cli.command(':confirm 3')
    assert posts==[{'approval':'private-approval'}] and 'private-approval' not in ''.join(out)


def test_cli_same_current_app_drives_chat_and_shared_memory_and_trace(api):
    store,kb,app,client=api
    model=client_with(response(tool='ask_clarification',args={'question':'请补充终端版本。'}))
    app.state.agent.client=model
    # Existing TestClient owns the app; all CLI operations use its real routes.
    client.headers['Authorization']='Bearer user-api';output=[]
    cli=DeskCLI(client,write=output.append,poll_seconds=.001)
    run=cli.chat('VPN故障')
    assert run['status']=='awaiting_input' and model.messages.create.await_count==1
    cli.command(':trace');cli.command(':recent');cli.command(':memory');cli.command(':integrations')
    assert any('knowledge_search' in item for item in output)
    assert store.get_run(USER,run['id'])['answer']=='请补充终端版本。'


def test_cli_rejects_insecure_remote_or_credential_urls():
    for value in ('http://example.com','https://user:secret@example.com','https://example.com?token=x'):
        with pytest.raises(ValueError):base_url(value)
    assert base_url('http://127.0.0.1:18080/')=='http://127.0.0.1:18080'


@pytest.mark.services
@pytest.mark.asyncio
async def test_current_batch_api_projects_real_chroma_and_invalidates_real_redis(postgres_store,vectors,cache):
    from resolvedesk.services import DeskServices
    from test_resolvedesk import FakeEmbedding
    kb=HybridKnowledge(postgres_store,FakeEmbedding(),vectors);model=client_with()
    services=DeskServices(postgres_store,kb,model,mcp_configs=[],coordination=cache)
    agent=DeskAgent(postgres_store,kb,model,services,cache)
    accounts={hashlib.sha256(name.encode()).hexdigest():vars(p) for name,p in (('support-token',SUPPORT),('user-token',USER))}
    app=create_app(postgres_store,kb,agent,accounts)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://localhost',headers={'Authorization':'Bearer support-token'}) as http:
        docs=[{'document_id':'public','title':'VPN policy','content':'# VPN\n\nVPN original checks'},{'document_id':'private','title':'VPN private','content':'# VPN\n\nVPN private checks','visibility':'private'}]
        added=await http.post('/knowledge/add',json={'documents':docs});assert added.status_code==200
        assert all(not d['index_pending'] for d in added.json()['results']) and vectors.count()==2
        async def search():
            result=await http.post('/knowledge/search',json={'query':'VPN','mode':'hybrid'},headers={'Authorization':'Bearer user-token'})
            result.raise_for_status();return result.json()
        first=await search();assert {d['document_id'] for d in first['items']}=={'public'}
        assert (await search())['cached']
        docs[0]['content']='# VPN\n\nVPN updated advice'
        changed=await http.post('/knowledge/batch',json={'documents':docs});assert changed.status_code==200
        newest=await search();assert not newest['cached'] and 'updated advice' in newest['items'][0]['content']
        assert newest['items'][0]['version']!=first['items'][0]['version']
        assert model.messages.create.await_count==0
    await cache.close()
