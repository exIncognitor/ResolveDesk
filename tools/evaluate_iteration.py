"""Same frozen contract against a chosen immutable Python engine and its venv.

Only --live enables paid quality calls, always through the original shared
PostgreSQL budget. All business schemas, collections and caches are synthetic.
Artifacts and sandbox state are retained for inspection, never auto-replayed.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]


def read_contract(root):
    manifest=json.loads((root/'evaluation/holdout_manifest_v1.json').read_text(encoding='utf-8'))
    for name,expected in manifest['sha256'].items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('Frozen input changed: '+name)
    return manifest


async def run(args):
    sys.path.insert(0,str(args.engine_root))
    from dotenv import load_dotenv
    from resolvedesk.settings import ServiceSettings
    from resolvedesk.store import Store,Principal,encode,digest
    from resolvedesk.retrieval import HybridKnowledge,LocalEmbedding
    from resolvedesk.vectors import ChromaVectors
    from resolvedesk.coordination import Coordination
    from core.paid_api import BudgetedAnthropic
    from resolvedesk.eval_state import EvalState
    contract=read_contract(args.suite_root)
    if args.output.exists():raise ValueError('Refuse to overwrite an evaluation report')
    if args.kind=='quality' and not args.live:raise ValueError('Paid quality run requires --live')
    load_dotenv(args.config_root/'.env',override=False)
    settings=ServiceSettings.load(args.config_root)
    os.environ.update(RESOLVEDESK_DATABASE_URL=settings.database_url,RESOLVEDESK_REDIS_URL=settings.redis_url,RESOLVEDESK_CHROMA_URL=settings.chroma_url)
    if not args.live:os.environ['RESOLVEDESK_PAID_API_ENABLED']='false'
    identity=uuid.uuid4().hex
    schema='iteration_eval_'+identity
    store=Store(settings.database_url,schema=schema)
    embedding=LocalEmbedding(args.config_root/'data/models/multilingual-e5-small')
    vectors=ChromaVectors(settings.chroma_url,embedding.identity,collection='iteration-eval-'+identity)
    coordination=Coordination(settings.redis_url,'iteration-eval-'+identity)
    def client_factory():return BudgetedAnthropic(api_key=os.getenv('ANTHROPIC_API_KEY',''),base_url='https://api.deepseek.com/anthropic',timeout=25)
    client=client_factory()
    before=client.ledger().summary()
    report={'kind':args.kind,'contract_id':digest(contract),'contract':contract,'engine_root':str(args.engine_root),
        'engine_sha256':{str(p.relative_to(args.engine_root)):hashlib.sha256(p.read_bytes()).hexdigest() for folder in ('resolvedesk','agents','core','tooling') for p in sorted((args.engine_root/folder).glob('*.py'))},
        'backend':'postgresql/redis/chroma/e5','schema':schema,'budget_before':before,'started':time.time(),
        'provenance':'Frozen synthetic held-out data. No production claims; deterministic assertions primary, same Flash Judge is auxiliary and correlated.'}
    kb=HybridKnowledge(store,embedding,vectors)
    support=Principal('demo','support','support');user=Principal('demo','learner')
    try:
        for name in contract['sha256']:
            if name.startswith('knowledge/'):
                path=args.suite_root/name;content=path.read_text(encoding='utf-8')
                kb.add(support,path.stem,content.splitlines()[0].lstrip('# '),content)
        if args.kind=='retrieval':
            cases=json.loads((args.suite_root/'evaluation/holdout_retrieval_v1.json').read_text(encoding='utf-8'))['cases']
            results=[]
            for mode in ('bm25','vector','hybrid'):
                for case in cases:
                    started=time.perf_counter()
                    found=await asyncio.to_thread(kb.search,user,case['query'],5,mode)
                    items=found['items'];targets=set(case['relevant_documents'])
                    documents=[item['document_id'] for item in items]
                    valid=kb.validate_citations(user,[item['citation'] for item in items])
                    ranks=[i+1 for i,document in enumerate(documents) if document in targets]
                    results.append({'id':case['id'],'mode':mode,'documents':documents,'recall_at_5':len(targets&set(documents))/len(targets),
                        'mrr_at_5':1/min(ranks) if ranks else 0,'citation_valid':len(valid)==len(items) and bool(items),
                        'span_valid':all(next((args.suite_root/name).read_text(encoding='utf-8') for name in contract['sha256'] if name=='knowledge/'+item['document_id']+'.md')[item['start']:item['end']]==item['content'] for item in items),
                        'latency_ms':(time.perf_counter()-started)*1000,'degraded':found['degraded']})
                print(json.dumps({'stage':'retrieval','mode':mode,'completed':len(results)},ensure_ascii=False),flush=True)
            report['results']=results
            report['metrics']={mode:{key:statistics.mean(r[key] for r in results if r['mode']==mode) for key in ('recall_at_5','mrr_at_5','citation_valid','span_valid','latency_ms')} for mode in ('bm25','vector','hybrid')}
        else:
            from resolvedesk.intent import ITIntentRecognizer
            from resolvedesk.eval_metrics import measure_intent,intent_report
            from resolvedesk.quality import QualityRunner
            from resolvedesk.eval_models import EvalDataset
            dataset=EvalDataset.model_validate(json.loads((args.suite_root/'evaluation/holdout_v1.json').read_text(encoding='utf-8'))).model_dump()
            state=EvalState(store,'iteration-'+identity)
            recognizer=ITIntentRecognizer(client,embedding)
            slots=asyncio.Semaphore(4)
            async def measure(case):
                async with slots:
                    async def operation():return await measure_intent(recognizer,case)
                    value=await state.unit('holdout-intent:'+case['id'],operation)
                    print(json.dumps({'stage':'intent','id':case['id']},ensure_ascii=False),flush=True)
                    return value
            observed=await asyncio.gather(*(measure(case) for case in dataset['intent_cases']))
            report['intent']=intent_report(observed)
            # Same single transient vector failure wrapper on both revisions.
            # It cannot affect live services: this is a separate process/schema.
            faults=json.loads((args.suite_root/'evaluation/holdout_faults_v1.json').read_text(encoding='utf-8'))['faults']
            injected=[];original=HybridKnowledge.search
            def with_fault(knowledge,p,query,top_k=5,mode='hybrid'):
                for fault in faults:
                    if p.user==fault['user_id'] and mode!='bm25' and fault['dialog_id'] not in injected:
                        injected.append(fault['dialog_id']);raise ConnectionError('Synthetic heldout vector outage')
                return original(knowledge,p,query,top_k,mode)
            HybridKnowledge.search=with_fault
            try:
                dialog_dataset={**dataset,'intent_cases':[]}
                runner=QualityRunner(store,kb,client_factory,coordination=coordination)
                result=await runner.run(support,'iteration-'+identity,12,True,
                    lambda done,total:print(json.dumps({'stage':'dialog','done':done,'total':total}),flush=True),dataset=dialog_dataset,state=state)
                report['quality']=result
                report['faults']={'required':[f['dialog_id'] for f in faults],'injected':injected,'all_injected':len(injected)==len(faults)}
            finally:HybridKnowledge.search=original
    except Exception as exc:
        report['error']=type(exc).__name__
        raise
    finally:
        report['budget_after']=client.ledger().summary();report['finished']=time.time()
        report['budget_note']='Cumulative shared ledger is never reset; failures and uncertain calls remain charged conservatively.'
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x',encoding='utf-8') as out:json.dump(report,out,ensure_ascii=False,indent=2)
        await client.close();await coordination.close();vectors.close();store.close()
        print(json.dumps({'report':str(args.output),'error':report.get('error'),'budget_after':report['budget_after']},ensure_ascii=False),flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--engine-root',type=Path,required=True)
    parser.add_argument('--config-root',type=Path,default=ROOT)
    parser.add_argument('--suite-root',type=Path,default=ROOT)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--kind',choices=['retrieval','quality'],required=True)
    parser.add_argument('--live',action='store_true')
    asyncio.run(run(parser.parse_args()))


if __name__=='__main__':main()
