from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from cryptography.fernet import Fernet
import pytest

from resolvedesk.business import BusinessService
from resolvedesk.identity import IdentityService
from resolvedesk.model_admin import ModelRegistry,UsageRecorder
from resolvedesk.store import Principal,Store
from test_database_backends import postgres_store
from test_it_workflows import USER,SUPPORT,ACCESS,REPAIR,submit,action


@pytest.fixture
def store(tmp_path):
    value=Store(tmp_path/'enterprise-workflows.sqlite3')
    BusinessService(value).seed_demo()
    yield value
    value.close()


def provision(service,store,fields,key):
    ticket=submit(store,fields,key=key)
    case=service.get(USER,ticket['id'])
    return action(service,SUPPORT,action(service,SUPPORT,case,'approve'),'provision')


def test_grant_sources_preserve_stronger_access_and_revoke_only_the_selected_source(store):
    service=BusinessService(store)
    long_read=provision(service,store,dict(ACCESS,duration_days=30),'long-read')
    write=provision(service,store,dict(ACCESS,access_level='write',duration_days=7),'short-write')
    short_read=provision(service,store,dict(ACCESS,duration_days=1),'short-read')
    grant=service.context(USER)['my_grants'][0]
    assert grant['level']=='write' and grant['source_ticket']==write['ticket_id']
    action(service,USER,short_read,'revoke')
    assert service.context(USER)['my_grants'][0]['level']=='write'
    action(service,USER,write,'revoke')
    grant=service.context(USER)['my_grants'][0]
    assert grant['level']=='read' and grant['source_ticket']==long_read['ticket_id']
    with store.db(True) as db: db.execute('UPDATE grant_sources SET expires=0')
    assert service.context(USER)['my_grants']==[]


def test_offboarding_revokes_sessions_grants_and_seats_in_one_transaction(store):
    identity=IdentityService(store)
    for p in (USER,SUPPORT): identity.create_account(SUPPORT,p.user,'synthetic-password-123',p.role)
    service=BusinessService(store,identity)
    provision(service,store,ACCESS,'offboard-access')
    fields={'workflow':'license_request','category':'software','product':'IntelliJ IDEA Ultimate','duration_days':3,
            'summary':'Synthetic license','description':'Synthetic use','priority':'normal'}
    ticket=submit(store,fields,key='offboard-license')
    case=service.get(USER,ticket['id'])
    for verb in ('approve','reserve','allocate'):case=action(service,SUPPORT,case,verb)
    token=identity.login('demo',USER.user,'synthetic-password-123')['token']
    identity.set_enabled(SUPPORT,USER.user,False)
    assert identity.authenticate(token) is None
    assert service.context(USER)['my_grants']==[]
    assert service.get(SUPPORT,ticket['id'])['status']=='revoked'
    assert service.context(SUPPORT)['licenses'][0]['available_seats']==2
    assert any(row['action']=='account.disable' for row in identity.audit_list(SUPPORT))


def test_reassign_uses_current_tenant_active_support_directory(store):
    identity=IdentityService(store)
    other=Principal('demo','operator2','support')
    for p in (USER,SUPPORT,other): identity.create_account(SUPPORT,p.user,'synthetic-password-123',p.role)
    service=BusinessService(store,identity)
    case=service.get(USER,submit(store,REPAIR,key='directory-repair')['id'])
    case=action(service,SUPPORT,case,'triage')
    with pytest.raises(ValueError): action(service,SUPPORT,case,'assign',assignee=USER.user)
    with pytest.raises(ValueError): action(service,SUPPORT,case,'assign',assignee='unknown')
    case=action(service,SUPPORT,case,'assign',assignee=other.user)
    assert 'start' not in service.get(SUPPORT,case['ticket_id'])['allowed_actions']
    case=action(service,other,case,'start')
    case=action(service,SUPPORT,case,'assign',assignee=SUPPORT.user)
    case=action(service,SUPPORT,case,'start')
    assert case['assignee']==SUPPORT.user


def test_usage_missing_rates_or_usage_is_unknown_and_failed_calls_are_preserved(store):
    models=ModelRegistry(store,Fernet.generate_key())
    profile={'id':'synthetic','revision':1,'model':'test','protocol':'openai','currency':'USD',
             'prices':{'input':None,'output':None,'cached_input':None}}
    observer=UsageRecorder(store,USER,'run:synthetic',profile)
    first=observer.begin();observer.finish(first,SimpleNamespace(usage=SimpleNamespace(input_tokens=2,output_tokens=1)))
    second=observer.begin();observer.finish(second)
    pending=observer.begin()
    report=models.usage(SUPPORT)
    assert report['unknown_cost_calls']==3 and report['estimated_costs']=={}
    assert {row['status'] for row in report['recent']}=={'started','completed','failed'}
    assert report['input_tokens']==2


@pytest.mark.services
def test_postgres_competing_grants_and_cross_worker_model_activation(postgres_store):
    store=postgres_store;service=BusinessService(store)
    cases=[]
    for index,level in enumerate(('read','write')):
        ticket=submit(store,dict(ACCESS,access_level=level),key='competing-grant-'+str(index))
        cases.append(action(service,SUPPORT,service.get(USER,ticket['id']),'approve'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda case:action(service,SUPPORT,case,'provision'),cases))
    assert service.context(USER)['my_grants'][0]['level']=='write'
    with store.db() as db: assert db.execute('SELECT level FROM desk_grants').fetchone()[0]=='write'
    key=Fernet.generate_key();a=ModelRegistry(store,key);b=ModelRegistry(store,key)
    profile={'name':'Synthetic','protocol':'openai','model':'test','base_url':'https://models.example/v1',
             'api_key':'controlled-key','currency':'USD','prices':{}}
    saved=a.save(SUPPORT,profile);a.activate(SUPPORT,saved['id'],1)
    assert b.active(USER)['model']=='test'
    a.activate(SUPPORT,'environment',0)
    assert b.active(USER)['id']=='environment'
