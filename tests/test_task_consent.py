"""Development grammar examples independent of the frozen live acceptance."""
import pytest

from resolvedesk.task_contract import is_cancellation, update, facts
from resolvedesk.agent import DeskAgent
from resolvedesk.services import DeskServices
from resolvedesk.business import BusinessService
from test_it_execution import model_for
from test_it_workflows import USER, SUPPORT, ACCESS
from test_iteration4_contracts import environment


@pytest.mark.parametrize('text',['取消我的权限申请','撤销这份草稿吧','请帮我放弃当前的软件席位申请。','麻烦取消之前的维修申请','先不处理了'])
def test_affirmative_cancellation_grammar(text):
    assert is_cancellation(text)


@pytest.mark.parametrize('text',['不要取消我的申请','请别取消这份草稿','我想了解取消权限申请的流程','申请取消不了怎么办','取消申请需要什么手续？'])
def test_cancellation_questions_and_negations_do_not_cancel(text):
    assert not is_cancellation(text)


@pytest.mark.parametrize('text',['只解释商业授权审批规则，不提交申请','不申请权限，仅查询我的资产','请说明维修申请流程，先别创建草稿'])
def test_consultation_does_not_grant_submission_consent(text):
    contract=update({'workflow':'access_request','consent':True},text,'hardware',4)
    assert not contract['consent']


def test_literal_request_type_is_independent_of_misrouted_domain():
    contract=update({},'请准备仓库权限申请','hardware',1)
    assert contract['workflow']=='access_request'
    assert facts({'fine_intent':'greeting'},contract)=='greeting'
    assert facts({'fine_intent':'asset_lookup'},contract)=='business'


@pytest.mark.asyncio
async def test_submitted_case_cancellation_does_not_claim_or_perform_a_reversal(environment):
    store,kb=environment
    run,_=store.create_run(USER,'请准备仓库权限申请','submitted-cancel')
    store.claim(USER,run['id']);store.prepare(USER,run['id'],ACCESS,[])
    draft=store.get_run(USER,run['id']);ticket=store.confirm(USER,run['id'],draft['approval'])
    before=BusinessService(store).get(USER,ticket['id'])
    store.reply(USER,run['id'],'撤销这份申请。','cancel-turn')
    model=model_for(fine=('access_request',));agent=DeskAgent(store,kb,model,DeskServices(store,kb,model,mcp_configs=[]))
    try:
        await agent.execute(USER,run['id']);current=store.get_run(USER,run['id'])
        assert current['status']=='completed'
        assert '没有撤销工单' in current['answer']
        assert BusinessService(store).get(USER,ticket['id'])==before
        assert store.confirm(USER,run['id'],draft['approval'])['id']==ticket['id']
        assert len(store.list_tickets(USER))==1
    finally:await agent.close()


def test_submitted_conversation_can_be_read_but_cannot_prepare_another_ticket(environment):
    store,_=environment
    run,_=store.create_run(USER,'申请','readonly-after-confirm')
    store.claim(USER,run['id']);store.prepare(USER,run['id'],ACCESS,[])
    draft=store.get_run(USER,run['id']);ticket=store.confirm(USER,run['id'],draft['approval'])
    store.reply(USER,run['id'],'查询当前进度','read-progress');store.claim(USER,run['id'])
    from resolvedesk.store import Conflict
    with pytest.raises(Conflict,match='submitted conversation'):
        store.prepare(USER,run['id'],ACCESS,[])
    assert store.get_ticket(USER,ticket['id'])['fields']==ACCESS
