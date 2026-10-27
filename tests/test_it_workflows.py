"""Business outcomes, authorization, races and restart safety; all data is synthetic."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import time

from fastapi.testclient import TestClient
import pytest

from resolvedesk.app import create_app
from resolvedesk.agent import DeskAgent
from resolvedesk.business import BusinessService
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.store import Conflict, Principal, Store
from test_resolvedesk import client_with, response

USER = Principal("demo", "learner")
SUPPORT = Principal("demo", "support", "support")
ACCESS = {"summary": "仓库访问申请", "description": "阅读项目代码进行学习", "category": "access", "priority": "normal", "workflow": "access_request", "resource_id": "repo-resolvedesk", "access_level": "read", "duration_days": 7}
REPAIR = {"summary": "键盘维修", "description": "键盘失灵，已尝试检查外接设备", "category": "hardware", "priority": "normal", "workflow": "device_repair", "asset_id": "LT-1001"}


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path/"desk.sqlite3")
    BusinessService(store).seed_demo()
    return store


def submit(store, fields=ACCESS, user=USER, key="workflow-run"):
    run, _ = store.create_run(user, "合成业务申请", key)
    store.claim(user, run["id"])
    store.prepare(user, run["id"], fields, [])
    draft = store.get_run(user, run["id"])
    return store.confirm(user, run["id"], draft["approval"])


def action(service, actor, case, verb, **kwargs):
    return service.transition(actor, case["ticket_id"], verb, case["version"], "test-"+verb+"-"+str(case["version"]), "合成处理说明", **kwargs)


def test_access_approval_is_distinct_from_provision_and_persists(store):
    ticket = submit(store)
    service = BusinessService(store)
    case = service.get(USER, ticket["id"])
    assert case["status"] == "pending_approval" and not service.context(USER)["my_grants"]
    with pytest.raises(Conflict): action(service, USER, case, "approve")
    with pytest.raises(Conflict): action(service, SUPPORT, case, "provision")
    case = action(service, SUPPORT, case, "approve")
    assert case["status"] == "approved" and not service.context(USER)["my_grants"]
    case = action(service, SUPPORT, case, "provision")
    assert case["status"] == "fulfilled"
    restarted = BusinessService(Store(store.path))
    assert restarted.get(USER, ticket["id"])["status"] == "fulfilled"
    grants = restarted.context(USER)["my_grants"]
    assert grants[0]["resource"] == "repo-resolvedesk" and grants[0]["level"] == "read"
    assert len(case["audit"]) == 3
    with store.db(True) as db: db.execute("UPDATE desk_grants SET expires=0")
    assert not service.context(USER)["my_grants"]


@pytest.mark.parametrize("fields", [ACCESS | {"access_level": "admin"}, ACCESS | {"duration_days": 31}, ACCESS | {"resource_id": "production-root"}, ACCESS | {"resource_id": "docs-internal", "access_level": "write"}])
def test_policy_rejection_rolls_back_ticket_and_business_write(store, fields):
    with pytest.raises(ValueError): submit(store, fields)
    assert not store.list_tickets(USER) and not BusinessService(store).list(USER)


def test_case_scoping_self_approval_and_conflicting_idempotency(store):
    service = BusinessService(store)
    ticket = submit(store)
    case = service.get(USER, ticket["id"])
    for actor in (Principal("demo", "other"), Principal("other", "support", "support")):
        with pytest.raises(KeyError): service.get(actor, ticket["id"])
        with pytest.raises(KeyError): action(service, actor, case, "approve")
    own = submit(store, user=SUPPORT, key="self-approval")
    with pytest.raises(Conflict): action(service, SUPPORT, service.get(SUPPORT, own["id"]), "approve")
    approved = service.transition(SUPPORT, ticket["id"], "approve", 1, "approve-repeat", "核对用途")
    assert service.transition(SUPPORT, ticket["id"], "approve", 1, "approve-repeat", "核对用途") == approved
    with pytest.raises(Conflict): service.transition(SUPPORT, ticket["id"], "reject", 1, "approve-repeat", "不同内容")
    with pytest.raises(Conflict): service.transition(SUPPORT, ticket["id"], "reject", 1, "new-approve-key", "过期页面")


def test_racing_approvals_cannot_both_win(store):
    ticket = submit(store)
    def attempt(verb):
        try:
            return BusinessService(Store(store.path)).transition(SUPPORT, ticket["id"], verb, 1, "race-key-"+verb, "并发评测")["status"]
        except Conflict: return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(attempt, ["approve", "reject"]))
    assert results.count("conflict") == 1
    assert len(BusinessService(store).get(USER, ticket["id"])["audit"]) == 2


def test_repair_rework_acceptance_and_asset_lock(store):
    ticket = submit(store, REPAIR)
    service = BusinessService(store)
    case = service.get(USER, ticket["id"])
    assert service.context(USER)["my_assets"][0]["status"] == "in_repair"
    with pytest.raises(Conflict): submit(store, REPAIR, key="duplicate-asset")
    with pytest.raises(ValueError): submit(store, REPAIR | {"asset_id": "LT-1002"}, key="other-asset")
    for verb in ("triage", "assign", "start", "resolve"):
        case = action(service, SUPPORT, case, verb, **({"assignee": "support"} if verb == "assign" else {}))
    with pytest.raises(Conflict): action(service, SUPPORT, case, "accept")
    case = action(service, USER, case, "reopen")
    assert case["status"] == "in_progress"
    case = action(service, SUPPORT, case, "resolve")
    case = action(service, USER, case, "accept")
    assert case["status"] == "closed" and service.context(USER)["my_assets"][0]["status"] == "available"
    assert len(case["audit"]) == 8
    assert store.get_ticket(USER, ticket["id"])["status"] == "closed"


@pytest.mark.parametrize("fields", [ACCESS, REPAIR])
def test_cancel_leaves_no_grant_and_releases_asset(store, fields):
    ticket = submit(store, fields)
    service = BusinessService(store)
    case = action(service, USER, service.get(USER, ticket["id"]), "cancel")
    assert case["status"] == "cancelled" and not service.context(USER)["my_grants"]
    assert service.context(USER)["my_assets"][0]["status"] == "available"


def test_http_contract_scopes_actions_and_catalog(store):
    ticket = submit(store)
    tokens = {"user": USER, "operator": SUPPORT, "stranger": Principal("demo", "stranger")}
    accounts = {hashlib.sha256(token.encode()).hexdigest(): vars(p) for token, p in tokens.items()}
    agent = DeskAgent(store, HybridKnowledge(store), client_with())
    with TestClient(create_app(store, agent.knowledge, agent, accounts)) as http:
        def headers(token): return {"Authorization": "Bearer "+token}
        assert http.get("/service-catalog").status_code == 401
        assert not http.get("/service-catalog", headers=headers("stranger")).json()["my_assets"]
        assert http.get("/service-cases/"+ticket["id"], headers=headers("stranger")).status_code == 404
        payload = {"action": "approve", "version": 1, "request_key": "http-approve", "note": "主管审核通过"}
        assert http.post(f"/service-cases/{ticket['id']}/actions", headers=headers("user"), json=payload).status_code == 409
        assert http.post(f"/service-cases/{ticket['id']}/actions", headers=headers("operator"), json=payload).json()["status"] == "approved"
        assert http.get("/metrics", headers=headers("user")).status_code == 403
        assert "resolvedesk_active_alerts" in http.get("/metrics", headers=headers("operator")).text


@pytest.mark.asyncio
async def test_agent_prepares_workflow_without_materializing_it(store):
    model = client_with(response(tool="prepare_ticket", args=ACCESS))
    agent = DeskAgent(store, HybridKnowledge(store), model)
    run, _ = store.create_run(USER, "请准备仓库权限申请", "agent-access-run")
    await agent.execute(USER, run["id"])
    result = store.get_run(USER, run["id"])
    assert result["status"] == "awaiting_confirmation" and result["draft"]["workflow"] == "access_request"
    assert not store.list_tickets(USER) and not agent.business.context(USER)["my_grants"]
