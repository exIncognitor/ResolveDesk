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
def test_actual_self_hosted_e5_has_same_vectors_and_auth_boundary():
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
        expected=LocalEmbedding(model_dir).embed(["VPN连接超时","IDEA授权"],True)
        actual=client.embed(["VPN连接超时","IDEA授权"],True)
        assert len(actual[0])==384 and all(abs(a-b)<1e-6 for x,y in zip(expected,actual) for a,b in zip(x,y))
    finally:
        if client:client.close()
        if worker.poll()is None:worker.terminate();worker.wait(timeout=10)
