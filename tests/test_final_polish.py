"""Development examples for object/purpose separation and concise risk guidance."""
import pytest
from resolvedesk.task_contract import request_workflow
from resolvedesk.agent import DeskAgent
from resolvedesk.services import DeskServices
from test_iteration4_contracts import environment
from test_it_execution import model_for
from test_it_workflows import USER,SUPPORT
from knowledge_fixtures import publish_fixture


@pytest.mark.parametrize('message,expected',[
    ('请准备IntelliJ IDEA Ultimate席位申请，用于阅读repo-resolvedesk代码，使用七天。','license_request'),
    ('申请docs-internal read权限，用途是核对LT-1001的维修文档。','access_request'),
    ('准备LT-1001维修申请，用来恢复repo-resolvedesk开发环境。','device_repair'),
    ('申请软件席位，同时申请仓库权限。','ambiguous'),
    ('申请IDEA Ultimate席位，用于开发并申请repo-resolvedesk权限。','ambiguous'),
])
def test_request_object_not_incidental_purpose(message,expected):
    assert request_workflow(message,'general')==expected


@pytest.mark.asyncio
async def test_hazard_quotes_relevant_safety_without_unrelated_repair_process(environment):
    store,kb=environment
    publish_fixture(kb,SUPPORT,'safety','安全与维修','# 安全\n\n设备冒烟时停止使用并断开充电，不要自行拆卸。\n\n# 维修\n\n维修流程 submitted triaged assigned resolved closed。')
    model=model_for(fine=('device_hazard',));services=DeskServices(store,kb,model,mcp_configs=[])
    agent=DeskAgent(store,kb,model,services)
    try:
        run,_=store.create_run(USER,'电脑冒烟，转人工IT。','polish-hazard')
        await agent.execute(USER,run['id']);result=store.get_run(USER,run['id'])
        assert result['status']=='completed' and '停止使用' in result['answer']
        assert 'submitted triaged' not in result['answer']
        assert '未连接真实人工队列' in result['answer'] and not store.list_tickets(USER)
    finally:await agent.close()
