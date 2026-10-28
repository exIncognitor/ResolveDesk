"""Frozen fourth-round Flash acceptance with original published corpus identities.

Public knowledge is read in a read-only transaction. All writes use fresh
schemas/collections; finance always uses the original shared budget ledger.
"""
import argparse
import asyncio
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def inputs():
    contract=json.loads((ROOT/'evaluation/iteration4_contract.json').read_text(encoding='utf-8'))
    for name,expected in contract['sha256'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=expected:raise ValueError('Frozen input changed: '+name)
    catalog=json.loads((ROOT/'knowledge/reference/catalog.json').read_text(encoding='utf-8'))
    paths={p.stem:p for p in (ROOT/'knowledge').glob('*.md')}
    paths.update({d['document_id']:ROOT/d['path'] for d in catalog['documents']})
    if len(paths)!=contract['corpus_documents']:raise ValueError('Unexpected corpus size')
    return contract,paths


def published_snapshot(url,paths):
    from sqlalchemy import create_engine,text
    engine=create_engine(url,isolation_level='REPEATABLE READ')
    try:
        with engine.begin() as db:
            db.execute(text('SET TRANSACTION READ ONLY'))
            evidence=[dict(r) for r in db.execute(text("SELECT * FROM public.chunks WHERE tenant='demo' ORDER BY id")).mappings() if r['document_id'] in paths]
            docs=[dict(r) for r in db.execute(text("SELECT * FROM public.knowledge_documents WHERE tenant='demo' ORDER BY document_id")).mappings() if r['document_id'] in paths]
            pubs=[dict(r) for r in db.execute(text("SELECT * FROM public.knowledge_publications WHERE tenant='demo' ORDER BY document_id")).mappings() if r['document_id'] in paths]
            clock=float(db.execute(text('SELECT EXTRACT(EPOCH FROM clock_timestamp())')).scalar_one())
        if {d['document_id'] for d in docs}!=set(paths) or {p['document_id'] for p in pubs}!=set(paths):raise ValueError('Public corpus does not match frozen inputs')
        for pub in pubs:
            doc=next(d for d in docs if d['document_id']==pub['document_id'])
            if pub['state']!='published' or pub['content_version']!=doc['version'] or not pub['effective_from']<=clock<pub['expires_at']:raise ValueError('Frozen corpus is not currently published')
        for row in evidence:
            content=paths[row['document_id']].read_text(encoding='utf-8')
            if row['visibility']!='tenant' or content[row['start']:row['end']]!=row['content']:raise ValueError('Public chunk differs from frozen original')
        if {r['document_id'] for r in evidence}!=set(paths):raise ValueError('Missing frozen evidence')
        return {'evidence':evidence,'documents':docs,'publications':pubs}
    finally:engine.dispose()


def copy_snapshot(store,kb,snapshot):
    from resolvedesk.store import Principal
    with store.db(True) as db:
        for table,rows in [('chunks',snapshot['evidence']),('knowledge_documents',snapshot['documents']),('knowledge_publications',snapshot['publications'])]:
            for row in rows:
                columns=list(row)
                db.execute('INSERT INTO '+table+'('+','.join('"'+c+'"' for c in columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(row[c] for c in columns))
        for d in snapshot['documents']:
            db.execute("INSERT INTO index_outbox VALUES(?,?,?,?,'pending',0,?,NULL,?)",(uuid.uuid4().hex,d['tenant'],d['document_id'],d['generation'],db.now(),db.now()))
    indexed=kb.drain_index(1000)
    if indexed['failed'] or indexed['pending']:raise ValueError('Copied source projection is incomplete')
    if len(kb.publications(Principal('demo','support','support')))!=len(snapshot['documents']):raise ValueError('Publication copy mismatch')


async def run(args):
    from dotenv import load_dotenv
    from resolvedesk.settings import ServiceSettings
    from resolvedesk.store import Store,Principal,digest
    from resolvedesk.retrieval import LocalEmbedding,HybridKnowledge
    from resolvedesk.vectors import ChromaVectors
    from resolvedesk.coordination import Coordination
    from resolvedesk.quality import QualityRunner
    from resolvedesk.eval_models import EvalDataset
    from resolvedesk.knowledge_governance import publication_identity
    from core.paid_api import BudgetedAnthropic
    load_dotenv(ROOT/'.env',override=False)
    settings=ServiceSettings.load(ROOT);contract,paths=inputs()
    total_cap=min(Decimal(contract['process_total_ceiling_cny']),Decimal(args.total_cap_cny or contract['process_total_ceiling_cny']))
    os.environ.update(RESOLVEDESK_DATABASE_URL=settings.database_url,RESOLVEDESK_REDIS_URL=settings.redis_url,
        RESOLVEDESK_CHROMA_URL=settings.chroma_url,RESOLVEDESK_RETRIEVAL_POLICY='always',RESOLVEDESK_EXECUTION_POLICY='full',
        RESOLVEDESK_TOTAL_BUDGET_CNY=str(total_cap),RESOLVEDESK_RESERVE_CNY=contract['reserve_cny'])
    snapshot=published_snapshot(settings.database_url,paths)
    if args.inspect:
        print(json.dumps({'documents':len(snapshot['documents']),'chunks':len(snapshot['evidence']),'publication_identity':digest(publication_identity(snapshot['publications'])),'paid_calls':0}));return
    def factory():return BudgetedAnthropic(api_key=os.getenv('ANTHROPIC_API_KEY',''),base_url='https://api.deepseek.com/anthropic',timeout=25)
    dataset=EvalDataset.model_validate_json((ROOT/contract['dataset']).read_text(encoding='utf-8')).model_dump()
    embedding=LocalEmbedding(ROOT/'data/models/multilingual-e5-small');monitor=factory()
    logging.disable(logging.INFO)
    try:
        for repeat in (1,2):
            output=ROOT/'reports'/f'iteration4-{args.label}-live-r{repeat}.json'
            marker=ROOT/'.codex-runtime'/f'iteration4-{args.label}-live-r{repeat}.started.json'
            if output.exists() or marker.exists():raise ValueError('Refuse to replay a completed or uncertain paid run')
            identity=uuid.uuid4().hex
            with marker.open('x',encoding='utf-8') as out:json.dump({'schema':'iteration4_live_'+identity,'started':time.time()},out)
            store=Store(settings.database_url,schema='iteration4_live_'+identity)
            vectors=ChromaVectors(settings.chroma_url,embedding.identity,collection='iteration4-live-'+identity)
            cache=Coordination(settings.redis_url,'iteration4-live-'+identity);kb=HybridKnowledge(store,embedding,vectors)
            report={'repeat':repeat,'label':args.label,'contract':contract,'budget_before':monitor.ledger().summary(),
                'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                'source_clean':not subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=ROOT,text=True).strip(),
                'corpus_sha256':{name:contract['sha256'][name] for name in contract['sha256'] if name.startswith('knowledge/')},
                'original_chunk_identity':digest(snapshot['evidence']),'publication_identity':digest(publication_identity(snapshot['publications'])),
                'source_schema':store.database.schema,'policies':contract['policies'],'model':'deepseek-flash','actual_process_total_ceiling_cny':str(total_cap)}
            started=time.time()
            try:
                copy_snapshot(store,kb,snapshot)
                runner=QualityRunner(store,kb,factory,coordination=cache)
                report['result']=await runner.run(Principal('demo','support','support'),'v4-'+identity,12,True,
                    lambda done,total:print(json.dumps({'repeat':repeat,'done':done,'total':total}),flush=True),dataset=dataset)
            except Exception as exc:
                report['execution_error']=type(exc).__name__
                raise
            finally:
                report.update(budget_after=monitor.ledger().summary(),elapsed_s=time.time()-started)
                with output.open('x',encoding='utf-8',newline='\n') as out:json.dump(report,out,ensure_ascii=False,indent=2)
                await cache.close();vectors.close();store.close()
                print(json.dumps({'report':str(output),'hard_passed':report.get('result',{}).get('deterministic_passed'),'judge_passed':report.get('result',{}).get('passed'),'budget':report['budget_after']}),flush=True)
    finally:await monitor.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--label',default='candidate-v1');parser.add_argument('--live',action='store_true');parser.add_argument('--inspect',action='store_true')
    parser.add_argument('--total-cap-cny',help='Optionally reduce the frozen process budget ceiling; never increase it')
    args=parser.parse_args()
    if not args.live and not args.inspect:parser.error('Use --inspect (free) or explicit --live (paid)')
    if not args.label.replace('-','').isalnum():parser.error('Invalid label')
    asyncio.run(run(args))
