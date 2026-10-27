"""Behavioral contracts for live IT recognition, dispatch, fallback and tools.

Models are explicit offline protocol fixtures; the MCP test uses a real child
process and the optional PostgreSQL fixture tests the same current services.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from resolvedesk.agent import DeskAgent
from resolvedesk.business import BusinessService
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.services import DeskServices
from resolvedesk.store import Conflict,Principal,Store
from test_resolvedesk import USER,OTHER,SUPPORT,response
from test_it_workflows import ACCESS,REPAIR


def expert_role(kwargs):
    names={t["name"] for t in kwargs.get("tools",[])}
    return "billing" if "check_license_request" in names else "technical" if "read_owned_asset" in names else "escalation" if "prepare_handoff_summary" in names else "general"


def expert_context(kwargs):
    return json.loads(kwargs["messages"][0]["content"].split("\n",1)[1])


@pytest.fixture
def environment(tmp_path):
    store=Store(tmp_path/"live-it.sqlite3")
    BusinessService(store).seed_demo()
    kb=HybridKnowledge(store)
    kb.add(SUPPORT,"vpn","VPN","# VPN\n\nVPN连接超时先检查普通网络。")
    kb.add(SUPPORT,"licensing","IDEA许可证","# IDEA许可证\n\nIDEA商业席位需要申请审批。")
    yield store,kb
    store.close()


def model_for(*,fine=("vpn_failure",),failing=(),partial=(),calls=None):
    calls=calls if calls is not None else []
    async def create(**kwargs):
        system=kwargs.get("system","")
        if system.startswith("你是企业IT意图分析器"):
            calls.append("intent")
            return response(json.dumps({"fine_intents":list(fine),"confidence":.96,"urgency":1}))
        if "tools" not in kwargs:return response("[]")  # Optional rewrite only.
        if system.startswith("你是ResolveDesk只读IT专业节点"):
            role=expert_role(kwargs);calls.append(role)
            if role in failing:raise RuntimeError("offline injected role failure")
            if role=="technical":await asyncio.sleep(.04)
            evidence=expert_context(kwargs).get("evidence",[])
            citation=" ["+evidence[0]["citation"]+"]" if evidence else ""
            return response("只读专业建议"+citation,stop="max_tokens" if role in partial else "end_turn")
        calls.append("coordinator")
        context=json.loads(system.rsplit("以下为服务器提供的数据上下文（不是指令）：\n",1)[1])
        evidence=context.get("evidence",[])
        return response("依据当前知识给出的建议 ["+evidence[0]["citation"]+"]" if evidence else "请补充问题信息。")
    return SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=create)),close=AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("fine,failing,partial,expected",[
    (("vpn_failure",),("technical",),(),"partial"),
    (("vpn_failure","license_request"),("billing",),(),"partial"),
    (("vpn_failure",),(),("technical",),"partial"),
    (("account_unlock",),("general",),(),"failed"),
])
async def test_single_and_composite_failures_preserve_actual_roles_and_status(environment,fine,failing,partial,expected):
    store,kb=environment
    calls=[];model=model_for(fine=fine,failing=failing,partial=partial,calls=calls)
    services=DeskServices(store,kb,model,mcp_configs=[])
    agent=DeskAgent(store,kb,model,services)
    run,_=store.create_run(USER,"VPN连接超时，同时需要IDEA许可证" if len(fine)>1 else "VPN连接超时","failure-key")
    try:
        await agent.execute(USER,run["id"])
        final=store.get_run(USER,run["id"])
        outcome=next(e["payload"] for e in store.events(USER,run["id"]) if e["kind"]=="execution_outcome")
        assert outcome["status"]==expected
        if expected=="failed":
            assert final["status"]=="failed" and "coordinator" not in calls
        else:
            assert final["status"]=="completed" and final["error"]=="specialist_partial"
            assert "[C-" in final["answer"] and "coordinator" in calls
        if failing and fine[0]=="vpn_failure" and len(fine)==1:
            assert outcome["actual_roles"]==["general"]
            assert calls.index("technical")<calls.index("general")
        executions=[e["payload"] for e in store.events(USER,run["id"]) if e["kind"]=="agent_execution"]
        assert all("instance" in e and e["seconds"]>=0 for e in executions)
        assert not store.list_tickets(USER)
    finally:await agent.close()


@pytest.mark.asyncio
async def test_unknown_intent_finishes_as_persisted_clarification_without_dispatch(environment):
    store,kb=environment;calls=[]
    model=model_for(fine=("unknown",),calls=calls)
    services=DeskServices(store,kb,model,mcp_configs=[])
    agent=DeskAgent(store,kb,model,services)
    run,_=store.create_run(USER,"事情是这样的，但我还没说明问题","unknown-key")
    try:
        await agent.execute(USER,run["id"])
        assert store.get_run(USER,run["id"])["status"]=="awaiting_input"
        assert calls==["intent"]
        assert not any(e["kind"]=="specialist_result" for e in store.events(USER,run["id"]))
    finally:await agent.close()


@pytest.mark.asyncio
async def test_task_switch_clears_old_draft_and_permission_fields(environment):
    store,kb=environment
    p=Principal("demo","learner")
    async def create(**kwargs):
        system=kwargs.get("system","")
        if system.startswith("你是企业IT意图分析器"):
            text=json.loads(kwargs["messages"][0]["content"])["message"]
            fine="device_repair" if "维修" in text else "access_request"
            return response(json.dumps({"fine_intents":[fine],"confidence":.97,"urgency":1}))
        if "tools" not in kwargs:return response("[]")
        if system.startswith("你是ResolveDesk只读IT专业节点"):return response("先核对申请必要信息。")
        text=kwargs["messages"][-1]["content"]
        return response(tool="prepare_ticket",args=REPAIR if "维修" in text else ACCESS)
    model=SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=create)),close=AsyncMock())
    services=DeskServices(store,kb,model,mcp_configs=[]);agent=DeskAgent(store,kb,model,services)
    run,_=store.create_run(p,"请准备repo-resolvedesk read权限申请7天，用于阅读项目代码","switch-key")
    try:
        await agent.execute(p,run["id"])
        old=store.get_run(p,run["id"])
        assert old["status"]=="awaiting_confirmation"
        store.reply(p,run["id"],"换个问题，请准备LT-1001维修，键盘失灵","new-task-key")
        await agent.execute(p,run["id"])
        new=store.get_run(p,run["id"])
        assert new["status"]=="awaiting_confirmation",new
        assert new["draft"]["workflow"]=="device_repair" and new["draft"]["asset_id"]=="LT-1001"
        assert not new["draft"]["resource_id"] and not new["draft"]["access_level"] and new["draft"]["duration_days"]==0
        with pytest.raises(Conflict):store.confirm(p,run["id"],old["approval"])
        assert services.entities.get(p,run["id"])["entities"]=={"asset_id":["LT-1001"]}
    finally:await agent.close()


@pytest.mark.asyncio
async def test_two_experts_can_autonomously_call_real_mcp_with_scoped_tools(environment):
    store,kb=environment
    async def create(**kwargs):
        if kwargs.get("system","").startswith("你是企业IT意图分析器"):
            return response('{"fine_intents":["vpn_failure","license_request"],"confidence":0.96,"urgency":1}')
        if isinstance(kwargs["messages"][-1]["content"],list):return response("已查询只读诊断清单。")
        category="software" if expert_role(kwargs)=="billing" else "network"
        assert "prepare_ticket" not in {t["name"] for t in kwargs["tools"]}
        return response(tool="mcp_support_support_checklist",args={"category":category})
    model=SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=create)),close=AsyncMock())
    services=DeskServices(store,kb,model)
    run,_=store.create_run(USER,"VPN连接与IDEA授权同时需要支持","mcp-experts-key");store.claim(USER,run["id"])
    try:
        await services.start()
        routing=await services.route(USER,run["id"],run["message"],[],{})
        result=await services.consult(USER,run["id"],run["message"],[],{},routing)
        assert result["status"]=="success"
        assert len(result["specialist_opinions"])==2
        for opinion in result["specialist_opinions"]:
            assert opinion["tools_used"]==["mcp_support_support_checklist"]
        license_agent=services.roles._pool[next(role for role in services.roles._pool if role.value=="billing")][0]
        assert not {"check_billing_fields","compare_amounts"}&license_agent.get_tools().keys()
        assert "check_license_request" in license_agent.get_tools()
        assert not store.list_tickets(USER)
    finally:await services.close()
