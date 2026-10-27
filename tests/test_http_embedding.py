import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

import httpx
import pytest

from resolvedesk.remote_embedding import HTTPEmbedding
from resolvedesk.retrieval import LocalEmbedding


def test_remote_embedding_preserves_space_and_validates_protocol():
    def respond(request):
        body=json.loads(request.content)
        assert body["query"] and body["input"]==["VPN","IDEA"]
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(200,json={"model":body["model"],"data":[{"index":1,"embedding":[0.,2.]},{"index":0,"embedding":[3.,0.]}]})
    client=HTTPEmbedding("http://embeddings.test","test-token-"+"x"*32,transport=httpx.MockTransport(respond))
    try:assert client.embed(["VPN","IDEA"],True)==[[1.,0.],[0.,1.]]
    finally:client.close()


@pytest.mark.parametrize("data",[[{"index":0,"embedding":[0.,0.]}],[{"index":0,"embedding":[True,1.]}],[{"index":1,"embedding":[1.,0.]}]])
def test_remote_embedding_rejects_invalid_vectors(data):
    transport=httpx.MockTransport(lambda request:httpx.Response(200,json={"model":LocalEmbedding.model_id+"@"+LocalEmbedding.revision,"data":data}))
    client=HTTPEmbedding("http://embeddings.test","test-token-"+"x"*32,transport=transport)
    try:
        with pytest.raises(ValueError):client.embed(["VPN"])
    finally:client.close()


@pytest.mark.services
def test_actual_self_hosted_e5_has_same_vectors_and_auth_boundary(tmp_path,monkeypatch):
    root=Path(__file__).resolve().parents[1]
    model_dir=root/"data/models/multilingual-e5-small"
    if os.getenv("RESOLVEDESK_SERVICE_TESTS")!="true" or not model_dir.is_dir():pytest.skip("Real pinned model verification explicitly disabled or model absent")
    with socket.socket() as probe:probe.bind(("127.0.0.1",0));port=probe.getsockname()[1]
    env=dict(os.environ);env["RESOLVEDESK_EMBEDDING_TOKEN"]=secrets.token_urlsafe(36)
    worker=subprocess.Popen([sys.executable,"-X","utf8","-B","-m","uvicorn","resolvedesk.embedding_service:app","--host","127.0.0.1","--port",str(port),"--no-access-log"],cwd=root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    base=f"http://127.0.0.1:{port}"
    client=None
    try:
        with httpx.Client(timeout=.5,trust_env=False) as health:
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                if worker.poll()is not None:raise RuntimeError("Embedding fixture exited")
                try:
                    if health.get(base+"/health").status_code==200:break
                except httpx.TransportError:pass
                time.sleep(.1)
            else:raise TimeoutError("Embedding fixture did not become ready")
            assert health.post(base+"/v1/embeddings",json={"model":"wrong","input":["VPN"]}).status_code==401
        client=HTTPEmbedding(base,env["RESOLVEDESK_EMBEDDING_TOKEN"])
        embedding=LocalEmbedding(model_dir)
        expected=embedding.embed(["VPN连接超时","IDEA授权"],True)
        actual=client.embed(["VPN连接超时","IDEA授权"],True)
        assert len(actual[0])==384 and all(abs(a-b)<1e-6 for x,y in zip(expected,actual) for a,b in zip(x,y))
        # Verify configuration reaches the live DeskServices router, not only
        # the standalone HTTP client. The LLM is a clearly offline fixture.
        import asyncio
        from resolvedesk.store import Store
        from resolvedesk.retrieval import HybridKnowledge
        from resolvedesk.services import DeskServices
        from test_resolvedesk import USER,client_with,response
        monkeypatch.setenv("RESOLVEDESK_INTENT_VECTOR_PROVIDER","self_hosted_http")
        monkeypatch.setenv("RESOLVEDESK_INTENT_VECTOR_URL",base)
        monkeypatch.setenv("RESOLVEDESK_EMBEDDING_TOKEN",env["RESOLVEDESK_EMBEDDING_TOKEN"])
        async def configured_route():
            store=Store(tmp_path/"remote-route.sqlite3")
            services=DeskServices(store,HybridKnowledge(store,embedding),client_with(response('{"fine_intents":["vpn_failure"],"confidence":0.95,"urgency":1}')),mcp_configs=[])
            run,_=store.create_run(USER,"VPN连接超时","remote-route-key")
            try:
                packet=await services.route(USER,run["id"],run["message"],[],{})
                assert packet["primary"]=="technical" and packet["source_scores"]["embedding"]["available"]
                assert packet["embedding_source"].startswith("self-hosted-http/")
            finally:await services.close();store.close()
        asyncio.run(configured_route())
    finally:
        if client:client.close()
        if worker.poll()is None:worker.terminate();worker.wait(timeout=10)
