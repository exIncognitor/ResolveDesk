"""Frozen repeated Flash conversations in isolated services, one shared ledger."""
import argparse
import asyncio
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]


async def run(args):
    sys.path.insert(0,str(args.engine_root))
    from dotenv import load_dotenv
    from resolvedesk.settings import ServiceSettings
    from resolvedesk.store import Store,Principal
    from resolvedesk.retrieval import LocalEmbedding,HybridKnowledge
    from resolvedesk.vectors import ChromaVectors
    from resolvedesk.coordination import Coordination
    from resolvedesk.quality import QualityRunner
    from resolvedesk.eval_models import EvalDataset
    from core.paid_api import BudgetedAnthropic
    load_dotenv(ROOT/'.env',override=False)
    settings=ServiceSettings.load(ROOT)
    os.environ.update(RESOLVEDESK_DATABASE_URL=settings.database_url,RESOLVEDESK_REDIS_URL=settings.redis_url,RESOLVEDESK_CHROMA_URL=settings.chroma_url,
        RESOLVEDESK_RETRIEVAL_POLICY='always',RESOLVEDESK_EXECUTION_POLICY='full')
    contract=json.loads((ROOT/'evaluation/iteration3_entity_contract.json').read_text(encoding='utf-8'))
    for name,expected in contract['sha256'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=expected:raise ValueError('Frozen input changed')
    # Reduce this process's authorized ceiling without changing the ledger,
    # migration state, original budget, or the running application's policy.
    os.environ['RESOLVEDESK_TOTAL_BUDGET_CNY']='11.146332'
    os.environ['RESOLVEDESK_RESERVE_CNY']='2'
    def factory():return BudgetedAnthropic(api_key=os.getenv('ANTHROPIC_API_KEY',''),base_url='https://api.deepseek.com/anthropic',timeout=25)
    monitor=factory();dataset=EvalDataset.model_validate_json((ROOT/'evaluation/iteration3_live_holdout.json').read_text(encoding='utf-8')).model_dump()
    embedding=LocalEmbedding(ROOT/'data/models/multilingual-e5-small')
    logging.disable(logging.INFO)
    try:
        for repeat in (1,2):
            output=ROOT/'reports'/f'iteration3-{args.label}-live-r{repeat}.json'
            if output.exists():raise ValueError('Refuse to overwrite or repeat a completed/uncertain paid run')
            identity=uuid.uuid4().hex;store=Store(settings.database_url,schema='iteration3_live_'+identity)
            vectors=ChromaVectors(settings.chroma_url,embedding.identity,collection='iteration3-live-'+identity)
            cache=Coordination(settings.redis_url,'iteration3-live-'+identity);kb=HybridKnowledge(store,embedding,vectors)
            before=monitor.ledger().summary();started=time.time()
            report={'engine_root':str(args.engine_root),'repeat':repeat,'label':args.label,'contract':contract,'budget_before':before,
                'project_budget_cny':'20','goal_start_commitment_cny':'6.146332','goal_extra_cap_cny':'3','process_total_ceiling_cny':'11.146332'}
            try:
                for name in contract['sha256']:
                    if name.startswith('knowledge/'):
                        path=ROOT/name;content=path.read_text(encoding='utf-8')
                        kb.add(Principal('demo','support','support'),path.stem,content.splitlines()[0].lstrip('# '),content)
                runner=QualityRunner(store,kb,factory,coordination=cache)
                report['result']=await runner.run(Principal('demo','support','support'),'v3-'+identity,8,True,
                    lambda done,total:print(json.dumps({'label':args.label,'repeat':repeat,'done':done,'total':total}),flush=True),dataset=dataset)
            except Exception as exc:
                report['execution_error']=type(exc).__name__
                raise
            finally:
                report.update(budget_after=monitor.ledger().summary(),elapsed_s=time.time()-started)
                with output.open('x',encoding='utf-8',newline='\n') as out:json.dump(report,out,ensure_ascii=False,indent=2)
                await cache.close();vectors.close();store.close()
                print(json.dumps({'report':str(output),'passed':report.get('result',{}).get('passed'),'total':report.get('result',{}).get('total'),'budget':report['budget_after']}),flush=True)
    finally:await monitor.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--engine-root',type=Path,required=True)
    parser.add_argument('--label',choices=['baseline','candidate'],required=True);parser.add_argument('--live',action='store_true')
    args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required; only Flash and the shared ledger are allowed')
    asyncio.run(run(args))


if __name__=='__main__':main()
