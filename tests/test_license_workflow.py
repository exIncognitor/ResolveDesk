"""Synthetic seat effects: confirmation, authorization, races, expiry and restart."""
from concurrent.futures import ThreadPoolExecutor
import hashlib

from fastapi.testclient import TestClient
import pytest

from resolvedesk.agent import DeskAgent,TOOLS
from resolvedesk.app import create_app
from resolvedesk.business import BusinessService
from resolvedesk.licenses import PRODUCT,expire_due
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.store import Conflict,Principal,Store
from test_database_backends import postgres_store
from test_it_workflows import USER,SUPPORT,submit,action
from test_resolvedesk import client_with,response

FIELDS={'summary':'IDEA商业授权申请','description':'用于后端接口联调与调试','category':'software','priority':'normal',
    'workflow':'license_request','product':PRODUCT,'duration_days':7}


@pytest.fixture
def store(tmp_path):
    store=Store(tmp_path/'license.sqlite3')
    yield store
    store.close()


def create(service,user=USER,key='license-request'):
    return service.get(user,submit(service.store,FIELDS,user,key)['id'])


def inventory(service,p=USER):return next(item for item in service.context(p)['licenses'] if item['product']==PRODUCT)


def test_confirm_approve_reserve_allocate_revoke_survives_restart(store):
    business=BusinessService(store);case=create(business)
    assert case['status']=='pending_approval' and inventory(business)['available_seats']==2
    with pytest.raises(Conflict):action(business,SUPPORT,case,'allocate')
    with pytest.raises(Conflict):action(business,USER,case,'approve')
    case=action(business,SUPPORT,case,'approve')
    assert case['status']=='approved' and case['license'] is None and inventory(business)['available_seats']==2
    case=action(business,SUPPORT,case,'reserve')
    assert case['status']=='reserved' and inventory(business)['reserved_seats']==1 and inventory(business)['allocated_seats']==0
    case=action(business,SUPPORT,case,'allocate')
    assert case['status']=='allocated' and inventory(business)['allocated_seats']==1 and inventory(business)['reserved_seats']==0
    reopened=Store(store.path)
    try:
        restarted=BusinessService(reopened)
        assert restarted.get(USER,case['ticket_id'])['license']['state']=='allocated'
        case=action(restarted,USER,case,'revoke')
        assert case['status']=='revoked' and inventory(restarted)['available_seats']==2
        assert len(case['audit'])==5 and store.get_ticket(USER,case['ticket_id'])['status']=='revoked'
    finally:reopened.close()


def test_self_approval_foreign_scope_and_current_version_are_enforced(store):
    business=BusinessService(store);case=create(business,SUPPORT)
    for verb in ('approve','reserve','allocate'):
        with pytest.raises(Conflict):action(business,SUPPORT,case,verb)
    case=create(business,USER,'scope-license')
    for actor in (Principal('demo','stranger'),Principal('foreign','support','support')):
        with pytest.raises(KeyError):business.get(actor,case['ticket_id'])
        with pytest.raises(KeyError):action(business,actor,case,'approve')
    approved=action(business,SUPPORT,case,'approve')
    with pytest.raises(Conflict):action(business,SUPPORT,case,'reserve')
    for verb in ('reserve','allocate'):
        with pytest.raises(Conflict):action(business,USER,approved,verb)
    assert inventory(business)['available_seats']==2


@pytest.mark.parametrize('change',[{'product':'Unapproved Product'},{'duration_days':31},{'duration_days':True},{'category':'hardware'}])
def test_bad_license_request_is_not_materialized(store,change):
    with pytest.raises(ValueError):submit(store,FIELDS|change)
    assert not store.list_tickets(USER) and not BusinessService(store).list(USER)


@pytest.mark.parametrize('allocated',[False,True])
def test_expiry_reclaims_seat_once_before_or_after_restart(store,allocated):
    business=BusinessService(store);case=create(business)
    for verb in ('approve','reserve')+ (('allocate',) if allocated else ()):
        case=action(business,SUPPORT,case,verb)
    with store.db(True) as db:db.execute('UPDATE license_allocations SET expires=0 WHERE ticket_id=?',(case['ticket_id'],))
    assert inventory(business)['available_seats']==2 and not inventory(business)['my_allocations']
    assert expire_due(store)==1 and expire_due(store)==0
    current=business.get(USER,case['ticket_id'])
    assert current['status']=='expired' and current['version']==case['version']+1
    assert current['license']['state']=='expired'
    assert current['audit'][-1]['actor']=='system:expiry'
    with pytest.raises(Conflict):action(business,SUPPORT,case,'allocate')


def test_expired_reservation_cannot_be_extended_after_another_case_takes_capacity(store):
    business=BusinessService(store);first=create(business)
    for verb in ('approve','reserve'):first=action(business,SUPPORT,first,verb)
    with store.db(True) as db:
        db.execute('UPDATE license_pools SET total=1')
        db.execute('UPDATE license_allocations SET expires=0 WHERE ticket_id=?',(first['ticket_id'],))
    other=Principal('demo','second')
    second=create(business,other,'second-license')
    for verb in ('approve','reserve'):second=action(business,SUPPORT,second,verb)
    with pytest.raises(Conflict):action(business,SUPPORT,first,'allocate')
    assert inventory(business)['reserved_seats']==1 and inventory(business)['available_seats']==0


def test_action_receipts_are_idempotent_and_cannot_change_payload(store):
    business=BusinessService(store);case=create(business)
    case=action(business,SUPPORT,case,'approve')
    args=(SUPPORT,case['ticket_id'],'reserve',case['version'],'same-reserve-key','模拟审批核验')
    reserved=business.transition(*args)
    assert business.transition(*args)==reserved
    with pytest.raises(Conflict):business.transition(*args[:-1],'另一个说明')
    assert len(reserved['audit'])==3 and inventory(business)['reserved_seats']==1


@pytest.mark.parametrize('state',['pending_approval','approved','reserved'])
def test_cancellation_does_not_leak_capacity(store,state):
    business=BusinessService(store);case=create(business)
    if state!='pending_approval':case=action(business,SUPPORT,case,'approve')
    if state=='reserved':case=action(business,SUPPORT,case,'reserve')
    case=action(business,USER,case,'cancel')
    assert case['status']=='cancelled' and inventory(business)['available_seats']==2


def assert_racing_reservations(store):
    business=BusinessService(store)
    cases=[create(business,Principal('demo','seat-user-'+str(i)),'seat-request-'+str(i)) for i in range(6)]
    cases=[action(business,SUPPORT,case,'approve') for case in cases]
    def reserve(case):
        try:return action(BusinessService(store),SUPPORT,case,'reserve')['status']
        except Conflict:return 'capacity_conflict'
    with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(reserve,cases))
    assert results.count('reserved')==2 and results.count('capacity_conflict')==4
    assert inventory(business)['available_seats']==0 and inventory(business)['reserved_seats']==2
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM license_allocations WHERE state='reserved'").fetchone()[0]==2
        assert db.execute("SELECT COUNT(*) FROM service_audit WHERE action='reserve'").fetchone()[0]==2


def test_sqlite_competing_cases_cannot_oversell(store):assert_racing_reservations(store)


@pytest.mark.services
def test_real_postgres_competing_cases_cannot_oversell(postgres_store):assert_racing_reservations(postgres_store)


def test_http_authorization_confirmation_and_license_action_contract(store):
    business=BusinessService(store);ticket=submit(store,FIELDS)
    accounts={hashlib.sha256(token.encode()).hexdigest():vars(actor) for token,actor in [('license-user',USER),('license-support',SUPPORT)]}
    kb=HybridKnowledge(store);agent=DeskAgent(store,kb,client_with())
    with TestClient(create_app(store,kb,agent,accounts)) as http:
        url='/service-cases/'+ticket['id']+'/actions'
        payload={'action':'reserve','version':1,'request_key':'http-reserve-license','note':'模拟业务测试'}
        assert http.post(url,json=payload).status_code==401
        assert http.post(url,json=payload,headers={'Authorization':'Bearer license-user'}).status_code==409
        payload.update(action='approve')
        approved=http.post(url,json=payload,headers={'Authorization':'Bearer license-support'}).json()
        payload.update(action='reserve',version=approved['version'],request_key='reserve-authorized')
        reserved=http.post(url,json=payload,headers={'Authorization':'Bearer license-support'})
        assert reserved.status_code==200 and reserved.json()['status']=='reserved'
        assert http.get('/service-catalog',headers={'Authorization':'Bearer license-user'}).json()['licenses'][0]['available_seats']==1


@pytest.mark.asyncio
async def test_agent_can_only_prepare_then_user_must_confirm(store):
    kb=HybridKnowledge(store);agent=DeskAgent(store,kb,client_with(response(tool='prepare_ticket',args=FIELDS)))
    try:
        run,_=store.create_run(USER,'请准备IDEA Ultimate席位申请，七天，用于接口联调','agent-license')
        await agent.execute(USER,run['id'])
        draft=store.get_run(USER,run['id'])
        assert draft['status']=='awaiting_confirmation' and draft['draft']['workflow']=='license_request'
        assert not store.list_tickets(USER) and inventory(BusinessService(store))['available_seats']==2
        assert '尚未提交' in draft['answer'] and '模拟' in draft['answer']
        assert not {'approve','reserve','allocate','revoke'}&{tool['name'] for tool in TOOLS}
        ticket=store.confirm(USER,run['id'],draft['approval'])
        assert BusinessService(store).get(USER,ticket['id'])['status']=='pending_approval'
    finally:await agent.close()
