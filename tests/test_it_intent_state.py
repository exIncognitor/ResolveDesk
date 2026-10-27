from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.intent_recognizer import UrgencyLevel
from resolvedesk.entity_state import EntityState
from resolvedesk.intent import ITIntentRecognizer,entities_from
from resolvedesk.store import Principal,Store
from test_resolvedesk import USER,OTHER,client_with,response


@pytest.mark.asyncio
async def test_actual_fusion_can_override_keywords_and_reports_source_weights():
    client=client_with(response('{"fine_intents":["license_request"],"confidence":0.96,"urgency":1,"reason":"授权咨询"}'))
    recognizer=ITIntentRecognizer(client)
    result=await recognizer.recognize("VPN")
    assert result.domain=="software_license" and result.confidence!=.9
    assert result.source_scores["llm"]["weight"]>.8 and result.source_scores["embedding"]["weight"]==0
    assert result.source_scores["pattern"]["scores"]["vpn_failure"]>.8
    assert not result.needs_clarification


@pytest.mark.asyncio
async def test_unknown_and_critical_are_explicit_and_failed_channel_not_forged():
    recognizer=ITIntentRecognizer(client_with())
    unknown=await recognizer.recognize("事情是这样的")
    assert unknown.needs_clarification and not unknown.source_scores["llm"]["available"]
    critical=await recognizer.recognize("LT-1001 笔记本电池鼓包")
    assert critical.urgency==UrgencyLevel.CRITICAL and critical.domains[0]=="escalation"
    assert not critical.needs_clarification


@pytest.mark.asyncio
async def test_multi_domain_results_preserve_multiple_experts():
    client=client_with(response('{"fine_intents":["vpn_failure","license_request"],"confidence":0.95,"urgency":2}'))
    result=await ITIntentRecognizer(client).recognize("VPN 连不上，同时 IDEA 许可证过期")
    assert result.domains==["network_endpoint","software_license"]
    assert result.fine_intent=="vpn_failure"


def test_entities_corrections_task_switch_and_literal_provenance(tmp_path):
    store=Store(tmp_path/"entities.sqlite3")
    state=EntityState(store)
    run,_=store.create_run(USER,"申请 repo-resolvedesk read 权限7天","state-test")
    initial=state.merge(USER,run["id"],run["message"],"account_access")
    assert initial["entities"]["duration_days"]==["7"]
    source=initial["sources"]["resource_id"]["turn_seq"]
    store.claim(USER,run["id"]);store.finish(USER,run["id"],"awaiting_input","补充")
    store.reply(USER,run["id"],"改成 write，14天","correction-key")
    changed=state.merge(USER,run["id"],"改成 write，14天","account_access")
    assert changed["entities"]["resource_id"]==["repo-resolvedesk"] and changed["entities"]["access_level"]==["write"]
    assert changed["sources"]["resource_id"]["turn_seq"]==source
    assert changed["sources"]["duration_days"]["turn_seq"]>source
    store.claim(USER,run["id"]);store.finish(USER,run["id"],"completed","完成")
    store.reply(USER,run["id"],"换个问题，维修LT-1001","switch-key")
    switched=state.merge(USER,run["id"],"换个问题，维修LT-1001","device_service")
    assert switched["reset"] and switched["entities"]=={"asset_id":["LT-1001"]}
    with pytest.raises(KeyError):state.get(OTHER,run["id"])
    assert "password" not in entities_from("密码是123456，apikey=not-a-real-key")
    store.close()
