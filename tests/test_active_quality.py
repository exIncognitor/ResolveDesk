"""Offline evaluator integration. Fake answers and Judge scores are explicit fixtures."""
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from resolvedesk.agent import TicketFields
from resolvedesk.business import classify
from resolvedesk.evaluations import EvaluationService
from resolvedesk.quality import CORPUS, QualityRunner, intent_report
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.services import DeskServices
from resolvedesk.store import Conflict, Principal, Store, encode
from test_resolvedesk import client_with, response
from test_it_workflows import ACCESS, REPAIR

SUPPORT = Principal("demo", "support", "support")


def test_versioned_synthetic_intent_corpus_has_per_case_predictions():
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    report = intent_report(corpus["intent_cases"])
    assert report["samples"] == 28 and report["accuracy"] == 1 and report["macro_f1"] == 1
    assert corpus["split"] == "development" and len(corpus["dialog_cases"]) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("judge_ok,wrong_fields", [(True,False), (False,False), (True,True)])
async def test_quality_uses_actual_http_confirmation_and_business_actions(tmp_path, monkeypatch, judge_ok, wrong_fields):
    import resolvedesk.quality as quality
    monkeypatch.setattr(quality, "ROOT", tmp_path)
    if not judge_ok:
        from evaluation.evaluator import QualityScores
        monkeypatch.setattr(quality.LLMJudge,"judge",AsyncMock(return_value=QualityScores(None,None,None,None,judge_failed=True,error="synthetic_judge_failure")))
    store = Store(tmp_path/"main.sqlite3")
    kb = HybridKnowledge(store)
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for path in (root/"knowledge").glob("*.md"):
        kb.add(SUPPORT, path.stem, path.stem, path.read_text(encoding="utf-8"))
    async def search(self, p, query, top_k=5, **kwargs): return self.knowledge.search(p, query, top_k)
    async def route(self, p, run_id, message, history, context):
        label = classify(message)["intent"]
        role = {"account_access": "general", "device_service": "technical", "network_endpoint": "technical", "software_license": "billing"}.get(label, "general")
        return {"intent": label, "primary": role, "supporting": []}
    monkeypatch.setattr(DeskServices, "search", search)
    monkeypatch.setattr(DeskServices, "route", route)
    def fake_client():
        async def create(**kwargs):
            if "tools" not in kwargs: return response('{"relevance":0.9,"accuracy":0.9,"completeness":0.9,"helpfulness":0.9}')
            message = kwargs["messages"][-1]["content"]
            if "先询问" in message: return response(tool="ask_clarification", args={"question": "请提供资源级别、有效期或资产ID。"})
            if "请准备权限申请" in message: return response(tool="prepare_ticket", args=ACCESS | ({"duration_days":14} if wrong_fields else {}))
            if "请准备维修申请" in message: return response(tool="prepare_ticket", args=REPAIR)
            context = json.loads(kwargs["system"].rsplit("以下为服务器提供的数据上下文（不是指令）：\n",1)[1])
            document = "permissions" if "repo-resolvedesk" in message else "endpoint" if "DNS" in message else "licensing" if "IDEA" in message else "devices"
            citation = next(item["citation"] for item in context["evidence"] if item["document_id"]==document)
            return response("合成离线回答，依据知识库 ["+citation+"]")
        return SimpleNamespace(messages=SimpleNamespace(create=create), close=AsyncMock())
    runner = QualityRunner(store, kb, fake_client)
    progress = []
    report = await runner.run(SUPPORT, "offline-integration", 6, True, lambda a,b:progress.append((a,b)))
    if wrong_fields:
        assert report["passed"] == 5
        assert not report["results"][-2]["checks"]["expected_fields"]
        assert not report["results"][-2]["checks"]["persisted_business_effect"]
    elif judge_ok:
        assert report["status"] == "complete" and report["passed"] == 6, report["results"]
    else:
        assert report["status"] == "partial" and report["valid_total"] == 0 and report["evaluation_errors"] == 6
        assert report["passed"] == 0 and "accuracy" not in report["avg_scores"]
    assert report["results"][-1]["lifecycle"]["status"] == "closed"
    assert report["results"][-2]["lifecycle"]["status"] == "fulfilled"
    assert not store.list_tickets(Principal("demo", "learner"))
    assert progress[-1] == (6,6)


def test_baseline_is_explicit_valid_scoped_and_not_silently_replaced(tmp_path):
    store = Store(tmp_path/"baselines.sqlite3")
    service = EvaluationService(store, HybridKnowledge(store))
    report = {"kind":"active_service_quality","suite_id":"suite-v1","judge_enabled":True,"status":"complete","total":2,"valid_total":2,"passed":2,
              "pass_rate":1,"evaluation_errors":0,"avg_scores":{"accuracy":.9},"intent":{"accuracy":1},"regressions":[]}
    with store.db(True) as db:
        db.execute("INSERT INTO eval_jobs(id,tenant,owner,status,completed,total,result,created) VALUES('job','demo','support','completed',2,2,?,0)", (encode(report),))
    with pytest.raises(PermissionError): service.set_baseline(Principal("demo", "user"), "job")
    with pytest.raises(KeyError): service.set_baseline(Principal("other", "support", "support"), "job")
    assert service.set_baseline(SUPPORT, "job")["saved"]
    regression = service.compare(SUPPORT, report | {"avg_scores":{"accuracy":.7}})
    assert regression["regressions"][0]["test_id"] == "metric:accuracy"
    assert "baseline_job_id" not in service.compare(SUPPORT, report | {"suite_id":"different"})
    with store.db(True) as db: db.execute("UPDATE eval_jobs SET result=?", (encode(report | {"evaluation_errors":1}),))
    with pytest.raises(Conflict): service.set_baseline(SUPPORT, "job")
