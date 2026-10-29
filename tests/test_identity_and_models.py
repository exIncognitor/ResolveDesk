import hashlib
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
import pytest

from resolvedesk.identity import IdentityService
from resolvedesk.model_admin import ModelRegistry,ManagedModelClient
from resolvedesk.store import Principal,Store
from resolvedesk.app import create_app
from resolvedesk.agent import DeskAgent
from resolvedesk.retrieval import HybridKnowledge
from core.model_client import model_scope,model_identity
from test_resolvedesk import client_with

SUPPORT=Principal('demo','support','support')
USER=Principal('demo','employee')
PASSWORD='synthetic-password-12345'
PROFILE={'name':'Controlled test','protocol':'openai','model':'model-a','base_url':'https://models.example/v1',
         'api_key':'never-return-this-provider-key','currency':'CNY','prices':{'input':'1','output':'2','cached_input':'0.1'}}


@pytest.fixture
def environment(tmp_path):
    store=Store(tmp_path/'identities.sqlite3')
    identity=IdentityService(store)
    for user,role in (('support','support'),('employee','user'),('learner','user')):
        identity.create_account(SUPPORT,user,PASSWORD,role)
    models=ModelRegistry(store,Fernet.generate_key())
    yield store,identity,models
    store.close()


def test_password_login_expiry_logout_disable_and_revocation(environment):
    store,identity,_=environment
    assert identity.login('demo','employee','incorrect') is None
    result=identity.login('demo','employee',PASSWORD)
    token=result['token']
    assert identity.authenticate(token)==USER
    with store.db() as db:
        row=db.execute('SELECT password_hash FROM desk_accounts WHERE user_id=?',('employee',)).fetchone()
        assert PASSWORD not in row[0] and row[0].startswith('pbkdf2_sha256$')
        assert db.execute('SELECT token_hash FROM desk_sessions').fetchone()[0]!=token
    identity.logout(token)
    assert identity.authenticate(token) is None
    token=identity.login('demo','employee',PASSWORD)['token']
    identity.revoke_sessions(SUPPORT,'employee')
    assert identity.authenticate(token) is None
    token=identity.login('demo','employee',PASSWORD)['token']
    identity.set_enabled(SUPPORT,'employee',False)
    assert identity.authenticate(token) is None and identity.login('demo','employee',PASSWORD) is None
    identity.set_enabled(SUPPORT,'employee',True)
    token=identity.login('demo','employee',PASSWORD)['token']
    with store.db(True) as db: db.execute('UPDATE desk_sessions SET expires=0')
    assert identity.authenticate(token) is None


def test_login_lockout_and_demo_switch_fails_closed_in_production(environment,monkeypatch):
    _,identity,_=environment
    for _ in range(5): assert identity.login('demo','employee','incorrect','peer') is None
    assert identity.login('demo','employee',PASSWORD,'peer') is None
    monkeypatch.setenv('RESOLVEDESK_DEMO_LOGIN_ENABLED','true')
    monkeypatch.setenv('RESOLVEDESK_ENV','production')
    with pytest.raises(PermissionError): identity.demo_login('support')
    monkeypatch.setenv('RESOLVEDESK_ENV','development')
    result=identity.demo_login('support')
    assert identity.authenticate(result['token'])==SUPPORT


def test_profiles_are_encrypted_redacted_tenant_scoped_and_version_checked(environment):
    store,_,models=environment
    with pytest.raises(PermissionError): models.save(USER,PROFILE)
    value=models.save(SUPPORT,PROFILE)
    assert PROFILE['api_key'] not in str(models.list(SUPPORT))
    with store.db() as db:
        cipher=db.execute('SELECT key_cipher FROM model_profiles').fetchone()[0]
        assert PROFILE['api_key'] not in cipher
    models.activate(SUPPORT,value['id'],value['revision'])
    assert models.describe(SUPPORT)['model']=='model-a'
    with pytest.raises(KeyError): models.activate(Principal('other','support','support'),value['id'],1)
    updated=models.save(SUPPORT,dict(PROFILE,api_key='',model='model-b'),value['id'],1)
    with pytest.raises(ValueError): models.save(SUPPORT,PROFILE,value['id'],1)
    assert models.active(SUPPORT)['key']==PROFILE['api_key']
    assert updated['revision']==2


@pytest.mark.asyncio
async def test_model_switch_pins_inflight_run_and_records_actual_tokens_without_caps(environment):
    store,_,models=environment
    a=models.save(SUPPORT,PROFILE);models.activate(SUPPORT,a['id'],1)
    managed=ManagedModelClient(models)
    async def transport(request):
        return httpx.Response(200,json={'choices':[{'message':{'content':'synthetic answer'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':100,'completion_tokens':20,'prompt_tokens_details':{'cached_tokens':10}}})
    async with model_scope(managed,USER,'run:synthetic') as bound:
        bound._http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
        b=models.save(SUPPORT,dict(PROFILE,model='model-b'));models.activate(SUPPORT,b['id'],1)
        assert managed.model=='model-a' and model_identity(managed)['model']=='model-a'
        await managed.messages.create(max_tokens=100,messages=[{'role':'user','content':'synthetic'}])
    async with model_scope(managed,USER,'run:next'):
        assert managed.model=='model-b'
    report=models.usage(SUPPORT)
    assert report['calls']==1 and report['unknown_cost_calls']==0
    assert report['estimated_costs']=={'CNY':'0.000131'}
    assert report['input_tokens']==100 and report['output_tokens']==20
    assert report['recent'][0]['owner']=='employee'
    assert models.usage(Principal('other','operator','support'))['calls']==0


def test_http_login_and_employee_cannot_reach_any_model_or_account_administration(environment,monkeypatch):
    store,identity,models=environment
    kb=HybridKnowledge(store);agent=DeskAgent(store,kb,client_with())
    monkeypatch.setenv('RESOLVEDESK_ENV','production')
    monkeypatch.setenv('RESOLVEDESK_DEMO_LOGIN_ENABLED','true')
    with TestClient(create_app(store,kb,agent,identity=identity,models=models)) as http:
        response=http.post('/auth/login',json={'username':'employee','password':PASSWORD})
        assert response.status_code==200 and response.headers['cache-control']=='no-store'
        token=response.json()['token'];headers={'Authorization':'Bearer '+token}
        assert http.get('/me',headers=headers).json()['role']=='user'
        for path in ('/admin/models','/admin/model-usage','/admin/accounts','/admin/audit'):
            assert http.get(path,headers=headers).status_code==403
        assert http.post('/admin/models',headers=headers,json=PROFILE).status_code==403
        assert http.post('/auth/demo-login',json={'role':'support'}).status_code==403
        assert http.get('/auth/options').json()['demo_login_enabled'] is False
        assert http.post('/auth/logout',headers=headers).status_code==200
        assert http.get('/me',headers=headers).status_code==401
