"""Free frozen same-corpus PG/Chroma/E5 retrieval comparison; no model calls."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--engine-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--dev',action='store_true')
    args=parser.parse_args();sys.path.insert(0,str(args.engine_root.resolve()))
    from resolvedesk.settings import ServiceSettings
    from resolvedesk.store import Store,Principal
    from resolvedesk.retrieval import LocalEmbedding,HybridKnowledge
    from resolvedesk.vectors import ChromaVectors
    if args.output.exists():raise ValueError('Evidence already exists')
    contract_path=ROOT/'evaluation/iteration3_knowledge_contract.json'
    contract=json.loads(contract_path.read_text(encoding='utf-8'))
    for name,expected in contract['sha256'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=expected:raise ValueError('Frozen input changed: '+name)
    catalog=json.loads((ROOT/'knowledge/reference/catalog.json').read_text(encoding='utf-8'))
    data=json.loads((ROOT/'evaluation'/('iteration3_retrieval_dev.json' if args.dev else 'iteration3_retrieval_holdout.json')).read_text(encoding='utf-8'))
    settings=ServiceSettings.load(ROOT);identity=uuid.uuid4().hex
    store=Store(settings.database_url,schema='iteration3_kb_'+identity)
    embedding=LocalEmbedding(ROOT/'data/models/multilingual-e5-small')
    vectors=ChromaVectors(settings.chroma_url,embedding.identity,collection='iteration3-kb-'+identity)
    kb=HybridKnowledge(store,embedding,vectors);p=Principal('kb-eval','reviewer','support')
    managed=hasattr(kb,'publication_action');results=[]
    try:
        docs=[{k:v for k,v in d.items() if k not in {'path','metadata'}}|{'content':(ROOT/d['path']).read_text(encoding='utf-8')}|
              ({'metadata':d['metadata']} if managed else {}) for d in catalog['documents']]
        kb.add_many(p,docs)
        if managed:
            for doc in kb.publications(p):
                kb.publication_action(p,doc['document_id'],'publish',doc['version'],doc['revision'],'Assistant reviewed official-source synthetic reference corpus; not enterprise policy.')
        for case in data['cases']:
            response=kb.search(p,case['query']);items=response['items']
            ranked=list(dict.fromkeys(i['document_id'] for i in items))
            rank=next((i+1 for i,key in enumerate(ranked) if key in case['relevant_documents']),None)
            valid=all(kb.citation(p,i['citation'])=={k:i[k] for k in kb.citation(p,i['citation'])} for i in items)
            spans=all((ROOT/next(d['path'] for d in catalog['documents'] if d['document_id']==i['document_id'])).read_text(encoding='utf-8')[i['start']:i['end']]==i['content'] for i in items)
            results.append({'id':case['id'],'rank':rank,'ranked':ranked,'recall_at_5':bool(rank and rank<=5),
                'reciprocal_rank_at_5':1/rank if rank and rank<=5 else 0,'positive':bool(case['relevant_documents']),
                'expect_sufficient':case['expect_sufficient'],'evidence_sufficient':response.get('evidence_sufficient'),
                'citations_valid':valid,'source_spans_valid':spans,'sources_attached':all(bool(i.get('publication',{}).get('metadata',{}).get('source_urls')) for i in items),
                'top_signals':[{k:i.get(k) for k in ('document_id','bm25','cosine')} for i in items[:3]]})
        positives=[r for r in results if r['positive']];negatives=[r for r in results if not r['positive']]
        report={'engine_root':str(args.engine_root),'paid_calls':0,'split':'development' if args.dev else 'frozen_acceptance',
            'contract_sha256':hashlib.sha256(contract_path.read_bytes()).hexdigest(),'corpus_documents':len(docs),
            'services':'PostgreSQL + Chroma HNSW + multilingual-e5-small','governance_supported':managed,
            'recall_at_5':sum(r['recall_at_5'] for r in positives)/len(positives),
            'mrr_at_5':sum(r['reciprocal_rank_at_5'] for r in positives)/len(positives),
            'positive_count':len(positives),'unsupported_count':len(negatives),
            'unsupported_false_sufficiency':sum(r['evidence_sufficient'] is True for r in negatives) if managed else None,
            'positive_sufficiency':sum(r['evidence_sufficient'] is True for r in positives) if managed else None,
            'all_citations_valid':all(r['citations_valid'] and r['source_spans_valid'] for r in results),'results':results,
            'limitations':'Synthetic questions and concise paraphrased official references. Ranked candidates alone do not imply a false generated answer. No paid generation tested. Isolated fixtures retained.'}
        with args.output.open('x',encoding='utf-8',newline='\n') as out:json.dump(report,out,ensure_ascii=False,indent=2)
        print(json.dumps({k:v for k,v in report.items() if k!='results'},ensure_ascii=False))
    finally:vectors.close();store.close()


if __name__=='__main__':main()
