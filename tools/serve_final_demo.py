"""Explicit isolated browser demonstration with the shared authoritative budget.

Creates retained synthetic service resources. Never writes public business data
or replaces credentials; the two fixed test tokens apply only to this loopback
fixture and are not deployment credentials.
"""
import argparse
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true');parser.add_argument('--port',type=int,default=8088)
    args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required; demo chat spends the shared budget')
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env',override=False)
    from resolvedesk.settings import ServiceSettings
    settings=ServiceSettings.load(ROOT)
    os.environ.update(RESOLVEDESK_TOTAL_BUDGET_CNY='13.76725',RESOLVEDESK_RESERVE_CNY='2',
        RESOLVEDESK_DATABASE_URL=settings.database_url,RESOLVEDESK_REDIS_URL=settings.redis_url,RESOLVEDESK_CHROMA_URL=settings.chroma_url,
        RESOLVEDESK_RETRIEVAL_POLICY='always',RESOLVEDESK_EXECUTION_POLICY='full')
    from core.paid_api import BudgetedAnthropic
    from resolvedesk.store import Store,Principal
    from resolvedesk.business import BusinessService
    from resolvedesk.retrieval import HybridKnowledge,LocalEmbedding
    from resolvedesk.vectors import ChromaVectors
    from resolvedesk.coordination import Coordination
    from resolvedesk.services import DeskServices
    from resolvedesk.agent import DeskAgent
    from resolvedesk.app import create_app
    from tools.evaluate_iteration4_live import inputs,published_snapshot,copy_snapshot
    marker=ROOT/'.codex-runtime/final-polish-ui-session.json'
    if marker.exists():raise ValueError('Demo session already exists; do not repeat uncertain paid steps')
    _,paths=inputs();source=published_snapshot(settings.database_url,paths)
    identity=uuid.uuid4().hex;schema='final_demo_'+identity
    store=Store(settings.database_url,schema=schema);BusinessService(store).seed_demo()
    embedding=LocalEmbedding(ROOT/'data/models/multilingual-e5-small')
    vectors=ChromaVectors(settings.chroma_url,embedding.identity,collection='final-demo-'+identity)
    cache=Coordination(settings.redis_url,'final-demo-'+identity);kb=HybridKnowledge(store,embedding,vectors);copy_snapshot(store,kb,source)
    client=BudgetedAnthropic(api_key=os.getenv('ANTHROPIC_API_KEY',''),base_url='https://api.deepseek.com/anthropic',timeout=25)
    services=DeskServices(store,kb,client,coordination=cache);agent=DeskAgent(store,kb,client,services,cache)
    accounts={hashlib.sha256(('final-demo-'+name).encode()).hexdigest():vars(Principal('demo',name,'support' if name=='support' else 'user')) for name in ('learner','support')}
    app=create_app(store,kb,agent,accounts);original=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(current):
        await services.start()
        try:
            async with original(current):yield
        finally:await cache.close();vectors.close();store.close()
    app.router.lifespan_context=lifespan
    report={'schema':schema,'collection':vectors.name,'redis_namespace':cache.namespace,'port':args.port,
        'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'corpus_documents':len(paths),'budget_before':client.ledger().summary(),'simulation':True,
        'note':'UI demonstration only; preserved fixture resources, original public business unchanged.'}
    with marker.open('x',encoding='utf8') as out:json.dump(report,out,ensure_ascii=False,indent=2)
    print(json.dumps({'session':str(marker),'port':args.port,'schema':schema}),flush=True)
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning',access_log=False)


if __name__=='__main__':main()
