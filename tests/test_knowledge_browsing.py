"""Document browsing must preserve publication, tenant, owner and version ACLs."""
import hashlib
import time

from fastapi.testclient import TestClient
import pytest

from resolvedesk.agent import DeskAgent
from resolvedesk.app import create_app
from resolvedesk.store import Principal
from test_knowledge_governance import knowledge,add,change,metadata
from test_resolvedesk import USER,SUPPORT,FOREIGN,client_with


def test_catalog_and_preview_do_not_expose_drafts_private_foreign_or_withdrawn(knowledge):
    kb=knowledge
    for name in ('visible','draft','withdrawn','private'):
        add(kb,doc=name,**({'visibility':'private'} if name=='private' else {}))
        if name!='draft':change(kb,doc=name)
    change(kb,'withdraw',doc='withdrawn')
    assert [row['document_id'] for row in kb.documents(USER)['items']]==['visible']
    assert {row['document_id'] for row in kb.documents(SUPPORT)['items']}=={'visible','private'}
    assert kb.documents(FOREIGN)['total']==0
    for principal,name in ((USER,'draft'),(USER,'private'),(USER,'withdrawn'),(FOREIGN,'visible')):
        with pytest.raises(KeyError):kb.document(principal,name)
    preview=kb.document(USER,'visible')
    assert preview['items'][0]['content'].startswith('# VPN')
    old_version=preview['items'][0]['version']
    kb.add(SUPPORT,'visible','Changed title','# VPN\nnew content',metadata=metadata())
    assert kb.documents(USER)['total']==0
    with pytest.raises(KeyError):kb.document(USER,'visible')
    change(kb,doc='visible')
    assert kb.document(USER,'visible')['items'][0]['version']!=old_version
    assert kb.documents(USER)['items'][0]['title']=='Changed title'


def test_catalog_time_boundaries_and_pagination(knowledge):
    kb=knowledge
    for name in ('a','b','c'):
        add(kb,doc=name);change(kb,doc=name)
    first=kb.documents(USER,limit=2)
    second=kb.documents(USER,limit=2,offset=2)
    assert first['total']==second['total']==3
    assert len(first['items'])==2 and len(second['items'])==1
    assert not {d['document_id'] for d in first['items']}&{d['document_id'] for d in second['items']}
    # Move publication windows in SQL; no sleeps or external clocks are needed.
    with kb.store.db(True) as db:
        db.execute("UPDATE knowledge_publications SET expires_at=? WHERE document_id='a'",(time.time()-10,))
        db.execute("UPDATE knowledge_publications SET effective_from=? WHERE document_id='b'",(time.time()+60,))
    assert [d['document_id'] for d in kb.documents(USER)['items']]==['c']
    for name in ('a','b'):
        with pytest.raises(KeyError):kb.document(USER,name)


def test_browse_api_authentication_limits_and_reader_management_separation(knowledge):
    kb=knowledge;add(kb);change(kb)
    accounts={hashlib.sha256(b'fixture-reader').hexdigest():{'tenant':USER.tenant,'user':USER.user,'role':'user'}}
    app=create_app(kb.store,kb,DeskAgent(kb.store,kb,client_with()),accounts=accounts)
    with TestClient(app) as api:
        assert api.get('/knowledge/documents').status_code==401
        headers={'Authorization':'Bearer fixture-reader'}
        assert api.get('/knowledge/documents',headers=headers).json()['total']==1
        assert api.get('/knowledge/documents/policy',headers=headers).json()['items']
        assert api.get('/knowledge/documents/missing',headers=headers).status_code==404
        for query in ('limit=0','limit=51','offset=-1'):
            assert api.get('/knowledge/documents?'+query,headers=headers).status_code==422
        assert api.get('/knowledge/documents/policy?limit=21',headers=headers).status_code==422
        assert api.get('/knowledge/publications',headers=headers).status_code==403
