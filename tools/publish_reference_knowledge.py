"""Explicit bounded import/review of the authored catalog and unchanged demos."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    catalog=json.loads((ROOT/'knowledge/reference/catalog.json').read_text(encoding='utf-8'))
    contract=json.loads((ROOT/'evaluation/iteration3_knowledge_contract.json').read_text(encoding='utf-8'))
    for name,expected in contract['sha256'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=expected:raise ValueError('Frozen reference corpus changed')
    if not args.apply:
        print(json.dumps({'official_references':len(catalog['documents']),'unchanged_simulated_handbooks':8,'default_state':'draft','review':'Assistant source review under user authorization; not independent enterprise review','apply_required':True}));return
    from resolvedesk.settings import ServiceSettings
    from resolvedesk.store import Store,Principal,encode
    from resolvedesk.retrieval import LocalEmbedding,HybridKnowledge
    from resolvedesk.vectors import ChromaVectors
    from tools.retain_evaluations import backup_local_compose
    settings=ServiceSettings.load(ROOT);store=Store(settings.database_url);vectors=None
    try:
        with store.db() as db:
            if db.execute("SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1").fetchone() or db.execute("SELECT 1 FROM eval_jobs WHERE status IN ('queued','running') LIMIT 1").fetchone():raise ValueError('Active public work blocks knowledge switch')
        archive=backup_local_compose(store)
        p=Principal('demo','support','support');docs=[];originals=[]
        for d in catalog['documents']:
            docs.append({k:v for k,v in d.items() if k!='path'}|{'content':(ROOT/d['path']).read_text(encoding='utf-8')})
        # Preserve exact original title/content/version. Refuse divergent user
        # material rather than overwriting it with a local learning example.
        with store.db() as db:
            for path in sorted((ROOT/'knowledge').glob('*.md')):
                current=db.execute('SELECT d.*,c.title FROM knowledge_documents d JOIN chunks c ON c.tenant=d.tenant AND c.document_id=d.document_id WHERE d.tenant=? AND d.document_id=? LIMIT 1',(p.tenant,path.stem)).fetchone()
                if not current:raise ValueError('Original handbook absent: '+path.stem)
                content=path.read_text(encoding='utf-8')
                stored=''.join(r['content'] for r in db.execute('SELECT content FROM chunks WHERE tenant=? AND document_id=? ORDER BY start',(p.tenant,path.stem)))
                if stored!=content or current['visibility']!='tenant':raise ValueError('Original differs; preserve user content: '+path.stem)
                # Legacy versions used a different content hash. Never rewrite
                # their version/chunk IDs merely to enroll publication metadata.
                originals.append({'document_id':path.stem,'version':current['version'],'metadata':{
                    'material_type':'simulated_policy','source_urls':[],'applicability':['ResolveDesk 模拟企业环境'],'domains':[{'access':'account_access','permissions':'account_access','devices':'device_service','endpoint':'network_endpoint','vpn':'network_endpoint'}.get(path.stem,'software_license')],
                    'source_checked_on':'2026-10-27','effective_from':1793059200,'expires_at':1800835200}})
        embedding=LocalEmbedding(ROOT/'data/models/multilingual-e5-small');vectors=ChromaVectors(settings.chroma_url,embedding.identity)
        kb=HybridKnowledge(store,embedding,vectors)
        result=kb.add_many(p,docs);reviewed=[]
        with store.db(True) as db:
            for original in originals:
                document=kb.governance.owner(db,p,original['document_id'])
                if document['version']!=original['version']:raise ValueError('Original changed during enrollment')
                kb.governance.import_metadata(db,p,original['document_id'],original['version'],original['metadata'])
                generation=document['generation']+1
                db.execute('UPDATE knowledge_documents SET generation=?,updated=? WHERE tenant=? AND document_id=?',(generation,db.now(),p.tenant,original['document_id']))
                kb.governance.enqueue(db,p,original['document_id'],generation)
        for doc in kb.publications(p):
            if doc['document_id'] not in {d['document_id'] for d in docs+originals}:continue
            note='Assistant reviewed official-source paraphrase under user authorization; technical reference, not enterprise policy.' if doc['metadata']['material_type']=='technical_reference' else 'Assistant labeled unchanged original handbook as simulated policy under user authorization; no claim of real enterprise policy.'
            reviewed.append(kb.publication_action(p,doc['document_id'],'publish',doc['version'],doc['revision'],note))
        index=kb.drain_index(100)
        report={'import':result,'original_versions_preserved':[{k:d[k] for k in ('document_id','version')} for d in originals],'publications':reviewed,'index':index,'backup':str(archive.path.relative_to(ROOT)),'backup_sha256':archive.sha256,
            'paid_calls':0,'note':'Original title/content/version preserved. Only demo tenant authored references and eight original demos reviewed. Financial ledger must never be rewound during recovery.'}
        output=ROOT/'reports'/('iteration3-publication-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
        with output.open('x',encoding='utf-8') as out:json.dump(report,out,ensure_ascii=False,indent=2)
        print(json.dumps({'published':len(reviewed),'index':index,'report':str(output.relative_to(ROOT)),'backup':report['backup'],'backup_sha256':archive.sha256},ensure_ascii=False))
    finally:
        if vectors:vectors.close()
        store.close()


if __name__=='__main__':main()
