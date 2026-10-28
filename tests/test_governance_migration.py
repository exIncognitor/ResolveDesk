"""Grandfather exact existing versions; every ingress for new IDs is draft."""
import hashlib
import json
from fastapi.testclient import TestClient
import pytest
from resolvedesk.agent import DeskAgent
from resolvedesk.app import create_app
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.store import Store
from test_it_workflows import USER,SUPPORT
from test_resolvedesk import client_with
from test_knowledge_governance import metadata


def test_exact_legacy_versions_survive_cutover_and_enroll_without_rechunking(tmp_path):
    store=Store(tmp_path/'legacy.sqlite3')
    with store.db(True) as db:
        db.execute('CREATE TABLE knowledge_documents(tenant TEXT,document_id TEXT,owner TEXT,visibility TEXT,version TEXT,generation INTEGER,updated REAL,PRIMARY KEY(tenant,document_id))')
        db.execute('CREATE TABLE chunks(id TEXT PRIMARY KEY,tenant TEXT,owner TEXT,visibility TEXT,document_id TEXT,title TEXT,version TEXT,section TEXT,start INTEGER,"end" INTEGER,content TEXT,vector TEXT,embedding TEXT)')
        text='VPN 原始内容'
        db.execute('INSERT INTO knowledge_documents VALUES(?,?,?,?,?,1,1)',('demo','legacy','support','tenant','old-content-hash'))
        db.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',('1'*24,'demo','support','tenant','legacy','VPN','old-content-hash','',0,len(text),text,None,None))
    kb=HybridKnowledge(store)
    try:
        source=kb.search(USER,'VPN',mode='bm25')['items'][0]
        assert source['citation']=='C-'+'1'*24 and 'publication' not in source
        row=kb.publications(SUPPORT)[0]
        assert row['state']=='legacy' and row['revision']==0 and row['readable']
        kb.add(SUPPORT,'new','VPN new','VPN 新内容')
        other=HybridKnowledge(store)
        assert {i['document_id'] for i in other.search(USER,'VPN',mode='bm25')['items']}=={'legacy'}
        kb.publication_action(SUPPORT,'legacy','metadata',row['version'],0,'Enroll unchanged legacy material',metadata())
        assert not kb.search(USER,'VPN',mode='bm25')['items']
        row=next(r for r in kb.publications(SUPPORT) if r['document_id']=='legacy')
        kb.publication_action(SUPPORT,'legacy','publish',row['version'],row['revision'],'Source reviewed')
        current=kb.citation(USER,source['citation'])
        assert current['content']==source['content'] and current['version']=='old-content-hash'
        assert current['publication']['metadata']['applicability']==['Windows 11']
    finally:store.close()


@pytest.mark.parametrize('ingress',['batch','batch_alias','single','txt','md','json_upload'])
def test_every_new_import_ingress_requires_review(tmp_path,ingress):
    store=Store(tmp_path/'ingress.sqlite3');kb=HybridKnowledge(store)
    app=create_app(store,kb,DeskAgent(store,kb,client_with()),{hashlib.sha256(b'ingress-support').hexdigest():vars(SUPPORT)})
    try:
        with TestClient(app) as client:
            client.headers['Authorization']='Bearer ingress-support'
            doc={'document_id':'new-'+ingress,'title':'VPN','content':'VPN 新导入内容'}
            if ingress in {'batch','batch_alias'}:result=client.post('/knowledge/add' if ingress=='batch' else '/knowledge/batch',json={'documents':[doc]})
            elif ingress=='single':result=client.post('/knowledge/documents',json=doc)
            else:
                filename='new.'+('json' if ingress=='json_upload' else ingress)
                content=json.dumps([doc]).encode() if ingress=='json_upload' else b'VPN new imported material'
                result=client.post('/knowledge/upload',files={'file':(filename,content)})
            assert result.status_code==200,result.text
            rows=kb.publications(SUPPORT)
            assert len(rows)==1 and rows[0]['state']=='draft' and not rows[0]['metadata_complete']
            assert kb.stats(USER)['total_chunks']==0 and not kb.search(USER,'VPN')['items']
            row=rows[0]
            completed=client.put('/knowledge/publications/'+row['document_id']+'/metadata',json={'version':row['version'],'revision':row['revision'],'note':'Source review','metadata':metadata()})
            assert completed.status_code==200
            row=completed.json()
            published=client.post('/knowledge/publications/'+row['document_id']+'/review',json={'version':row['version'],'revision':row['revision'],'action':'publish','note':'Approved reference'})
            assert published.status_code==200 and kb.search(USER,'VPN',mode='bm25')['items']
    finally:store.close()
