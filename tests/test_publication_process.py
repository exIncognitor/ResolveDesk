"""A separate worker must invalidate a warmed Redis retrieval after withdrawal."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys

import pytest

from resolvedesk.retrieval import HybridKnowledge
from test_database_backends import postgres_store
from test_chroma_projection import vectors
from test_redis_coordination import cache
from test_knowledge_governance import add,change
from test_resolvedesk import FakeEmbedding


WORKER=r'''
import asyncio,json,sys
from pathlib import Path
sys.path.insert(0,str(Path('tests').resolve()))
from resolvedesk.coordination import Coordination
from resolvedesk.settings import ServiceSettings
from resolvedesk.store import Store
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.vectors import ChromaVectors
from resolvedesk.reliable_retrieval import ReliableRetrieval
from tooling.tool_manager import MCPToolManager
from test_resolvedesk import FakeEmbedding,USER,client_with
async def main():
    settings=ServiceSettings.load();store=Store(settings.database_url,schema=sys.argv[1])
    vectors=ChromaVectors(settings.chroma_url,FakeEmbedding.identity,collection=sys.argv[2])
    cache=Coordination(settings.redis_url,sys.argv[3]);kb=HybridKnowledge(store,FakeEmbedding(),vectors)
    rag=ReliableRetrieval(kb,client_with(),MCPToolManager(api_key='unused'),cache)
    try:
        await rag.search(USER,'VPN',optimize=False)
        warm=await rag.search(USER,'VPN',optimize=False)
        print('READY '+json.dumps({'cached':warm['cached'],'count':len(warm['items'])}),flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        current=await rag.search(USER,'VPN',optimize=False)
        print('RESULT '+json.dumps({'count':len(current['items'])}),flush=True)
    finally:await cache.close();vectors.close();store.close()
asyncio.run(main())
'''


@pytest.mark.services
def test_actual_second_process_cannot_serve_withdrawn_cached_material(postgres_store,vectors,cache):
    kb=HybridKnowledge(postgres_store,FakeEmbedding(),vectors);add(kb);change(kb)
    root=Path(__file__).resolve().parents[1]
    process=subprocess.Popen([sys.executable,'-X','utf8','-B','-c',WORKER,postgres_store.database.schema,vectors.name,cache.namespace],
        cwd=root,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            ready=pool.submit(process.stdout.readline).result(timeout=30)
        assert ready.startswith('READY '), 'Worker did not initialize'
        warm=json.loads(ready[6:]);assert warm=={'cached':True,'count':1}
        change(kb,'withdraw')
        output,_=process.communicate('\n',timeout=30)
        assert process.returncode==0 and output.startswith('RESULT ')
        assert json.loads(output[7:])=={'count':0}
    finally:
        if process.poll() is None:process.terminate();process.wait(timeout=10)
