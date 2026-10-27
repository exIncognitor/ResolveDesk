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


@pytest.mark.asyncio
async def test_versioned_synthetic_intent_corpus_has_observed_predictions():
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    from resolvedesk.intent import ITIntentRecognizer
    from resolvedesk.eval_metrics import measure_intent
    model=client_with(response('{"fine_intents":["vpn_failure"],"confidence":0.99,"urgency":1}'),response('{"fine_intents":["vpn_failure"],"confidence":0.99,"urgency":1}'))
    recognizer=ITIntentRecognizer(model,None)
    results=[await measure_intent(recognizer,{'id':str(i),'message':message,'expected_intent':expected}) for i,(message,expected) in enumerate([('你好','network_endpoint'),('再见','software_license')])]
    report = intent_report(results)
    assert report['accuracy']==.5 and report['macro_f1']==pytest.approx(1/3)
    assert report['channel_coverage']=={'llm':2,'embedding':0,'pattern':2}
    assert model.messages.create.call_count==2 and len(corpus['intent_cases'])==28
    assert corpus["split"] == "development" and len(corpus["dialog_cases"]) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("judge_ok,wrong_fields,early_bad", [(True,False,False), (False,False,False), (True,True,False), (True,False,True)])
async def test_quality_uses_actual_http_confirmation_and_business_actions(tmp_path, monkeypatch, judge_ok, wrong_fields,early_bad):
    import resolvedesk.quality as quality
    monkeypatch.setattr(quality, "ROOT", tmp_path)
    from evaluation.evaluator import QualityScores
    async def judge(question,answer,context):
        if not judge_ok:return QualityScores(None,None,None,None,judge_failed=True,error='synthetic_judge_failure')
        score=.1 if early_bad and question=='我要申请仓库权限，请先询问缺少的必要信息。' else .9
        return QualityScores(score,score,score,score)
    judged=AsyncMock(side_effect=judge)
    monkeypatch.setattr(quality.LLMJudge,'judge',judged)
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
        if '鼓包' in message:role='escalation'
        return {"intent": label, "primary": role, "supporting": []}
    monkeypatch.setattr(DeskServices, "search", search)
    monkeypatch.setattr(DeskServices, "route", route)
    def fake_client():
        async def create(**kwargs):
            if "tools" not in kwargs: return response('{"relevance":0.9,"accuracy":0.9,"completeness":0.9,"helpfulness":0.9}')
            if kwargs.get("system","").startswith("你是ResolveDesk只读IT专业节点"):
                return response("合成专业节点只读建议。")
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
    elif early_bad:
        assert report['passed']==5 and not report['results'][-2]['turns'][0]['passed']
        assert report['results'][-2]['turns'][-1]['passed']
    elif judge_ok:
        assert report["status"] == "complete" and report["passed"] == 6, report["results"]
    else:
        assert report["status"] == "partial" and report["valid_total"] == 0 and report["evaluation_errors"] == 6
        assert report["passed"] == 0 and "accuracy" not in report["avg_scores"]
    assert report["results"][-1]["lifecycle"]["status"] == "closed"
    assert report["results"][-2]["lifecycle"]["status"] == "fulfilled"
    assert not store.list_tickets(Principal("demo", "learner"))
    assert progress[-1] == (34,34) and judged.call_count==8
    assert report['intent']['implementation']=='ITIntentRecognizer.recognize'


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


def test_baseline_can_preserve_valid_imperfect_measurements_and_compare_engine_versions(tmp_path):
    from copy import deepcopy
    from resolvedesk.quality import suite_identity
    from evaluation.evaluator import LLMJudge
    judge=LLMJudge(client_with(),'deepseek-flash',max_attempts=1)
    manifest={'dataset':{'version':'fixed'},'evidence':[],'backend':'postgresql','engine_sources':{'agent.py':'before'}}
    changed=deepcopy(manifest);changed['engine_sources']['agent.py']='after'
    suite=suite_identity(manifest,6,True,'embedding-v1',judge)
    assert suite==suite_identity(changed,6,True,'embedding-v1',judge)
    assert suite!=suite_identity(changed,6,True,'embedding-v2',judge)
    changed['dataset']['version']='new-labels'
    assert suite!=suite_identity(changed,6,True,'embedding-v1',judge)
    store=Store(tmp_path/'imperfect.sqlite3');service=EvaluationService(store,HybridKnowledge(store))
    report={'kind':'active_service_quality','suite_id':suite,'judge_enabled':True,'total':2,'valid_total':2,'passed':1,'pass_rate':.5,
        'evaluation_errors':0,'avg_scores':{'accuracy':.8,'intent_accuracy':.9},'intent':{'accuracy':.9},'engine_identity':'before'}
    with store.db(True) as db:db.execute("INSERT INTO eval_jobs(id,tenant,owner,status,completed,total,result,created) VALUES('valid','demo','support','completed',2,2,?,0)",(encode(report),))
    assert service.set_baseline(SUPPORT,'valid')['saved']
    compared=service.compare(SUPPORT,report|{'engine_identity':'after','avg_scores':{'accuracy':.6,'intent_accuracy':.7}})
    assert {r['test_id'] for r in compared['regressions']}=={'metric:accuracy','metric:intent_accuracy'}
