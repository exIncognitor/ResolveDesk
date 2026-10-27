"""Quality evaluation through the active ASGI APIs in an isolated local sandbox."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import secrets
import re
import statistics
import time
import uuid

import httpx

from evaluation.evaluator import LLMJudge
from resolvedesk.business import BusinessService, classify
from resolvedesk.store import Principal, Store, digest, encode

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evaluation" / "it_service_cases.json"


def intent_report(cases):
    details = [{**case, "prediction": classify(case["message"])["intent"]} for case in cases]
    labels = sorted({c["expected"] for c in cases} | {c["prediction"] for c in details})
    per_class = {}
    for label in labels:
        tp = sum(c["expected"] == label == c["prediction"] for c in details)
        predicted = sum(c["prediction"] == label for c in details)
        expected = sum(c["expected"] == label for c in details)
        precision, recall = tp/predicted if predicted else 0, tp/expected if expected else 0
        per_class[label] = {"precision": precision, "recall": recall, "f1": 2*precision*recall/(precision+recall) if precision+recall else 0}
    return {"samples": len(details), "accuracy": sum(c["prediction"] == c["expected"] for c in details)/len(details),
            "macro_f1": statistics.mean(v["f1"] for v in per_class.values()), "per_class": per_class, "results": details}


class QualityRunner:
    def __init__(self, store, knowledge, client_factory=None):
        self.store, self.knowledge, self.client_factory = store, knowledge, client_factory

    async def run(self, p, job_id, limit, use_judge, progress):
        from core.paid_api import BudgetedAnthropic
        from resolvedesk.agent import DeskAgent
        from resolvedesk.app import create_app
        from resolvedesk.retrieval import HybridKnowledge
        from resolvedesk.services import DeskServices
        if p.tenant != "demo": raise ValueError("合成业务评测当前仅适用于 demo 租户")
        corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
        cases = corpus["dialog_cases"][:limit]
        scratch = ROOT / ".codex-runtime" / ("quality-"+job_id)
        scratch.mkdir(parents=True, exist_ok=False)
        sandbox = Store(scratch/"sandbox.sqlite3")
        kb = HybridKnowledge(sandbox, self.knowledge.embedding)
        # Copy only tenant-visible evidence. Private documents and real business records stay out.
        with self.store.db() as source, sandbox.db(True) as target:
            rows = source.execute("SELECT * FROM chunks WHERE tenant=? AND visibility='tenant' ORDER BY id", (p.tenant,)).fetchall()
            if not rows: raise ValueError("需要先导入模拟知识库")
            columns = rows[0].keys()
            for row in rows:
                target.execute("INSERT INTO chunks("+",".join('"'+column+'"' for column in columns)+") VALUES("+",".join("?" for _ in columns)+")", tuple(row))
        BusinessService(sandbox).seed_demo()
        import os
        client = self.client_factory() if self.client_factory else BudgetedAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""), base_url="https://api.deepseek.com/anthropic", timeout=25)
        services = DeskServices(sandbox, kb, client)
        agent = DeskAgent(sandbox, kb, client, services)
        actors = {"learner": Principal("demo", "learner"), "support": Principal("demo", "support", "support")}
        tokens = {name: secrets.token_urlsafe(32) for name in actors}
        accounts = {hashlib.sha256(tokens[name].encode()).hexdigest(): vars(actor) for name, actor in actors.items()}
        api = create_app(sandbox, kb, agent, accounts)
        judge = LLMJudge(client, "deepseek-flash")
        judge._max_tokens = 512
        suite_id = digest({"corpus": corpus, "limit": limit, "judge": use_judge, "threshold": .75,
                           "judge_prompt": judge.JUDGE_PROMPT, "few_shot": judge.few_shot_prompt(),
                           "engine": "active-asgi-v1", "evidence": [tuple(r) for r in rows]})
        details = []
        try:
            await services.start()
            async with api.router.lifespan_context(api), httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://quality.local", timeout=120) as http:
                async def request(actor, method, path, payload=None):
                    response = await http.request(method, path, json=payload, headers={"Authorization": "Bearer "+tokens[actor]})
                    response.raise_for_status()
                    return response.json()

                async def wait(run_id):
                    async with asyncio.timeout(110):
                        while True:
                            run = await request("learner", "GET", "/runs/"+run_id)
                            if run["status"] not in {"running", "queued"}: return run
                            await asyncio.sleep(.05)

                for index, case in enumerate(cases):
                    before_tickets = len(sandbox.list_tickets(actors["learner"]))
                    detail = {"test_id": case["id"], "passed": False, "metadata": {}, "scores": {}}
                    try:
                        run = await request("learner", "POST", "/runs", {"message": case["turns"][0], "request_key": case["id"]+"-"+job_id})
                        run = await wait(run["id"])
                        turn_states = [run["status"]]
                        for turn in case["turns"][1:]:
                            await request("learner", "POST", f"/runs/{run['id']}/reply", {"message": turn,"request_key":"quality-"+uuid.uuid4().hex})
                            run = await wait(run["id"])
                            turn_states.append(run["status"])
                        with sandbox.db() as db:
                            events = [dict(r, payload=json.loads(r["payload"])) for r in db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run["id"],))]
                        routing = [e["payload"] for e in events if e["kind"] == "routing"]
                        checks = {"run_status": run["status"] == case["expected_status"], "role": bool(routing) and routing[-1]["primary"] == case["expected_role"],
                                  "no_unconfirmed_write": len(sandbox.list_tickets(actors["learner"])) == before_tickets}
                        if len(turn_states) > 1: checks["clarified_first"] = turn_states[0] == "awaiting_input"
                        cited = [e["payload"] for e in events if e["kind"] == "retrieval"]
                        evidence_documents = {kb.citation(actors["learner"], cid)["document_id"] for event in cited for cid in event.get("citations", [])}
                        cited_documents = {kb.citation(actors["learner"], cid)["document_id"] for cid in re.findall(r"C-[a-f0-9]{24}", run["answer"])}
                        checks["retrieved_evidence"] = set(case["documents"]).issubset(evidence_documents)
                        if not case.get("workflow"): checks["cited_answer"] = bool(cited_documents & set(case["documents"]))
                        lifecycle = None
                        if case.get("workflow"):
                            checks["workflow"] = bool(run.get("draft")) and run["draft"].get("workflow") == case["workflow"]
                            checks["expected_fields"] = all((run.get("draft") or {}).get(key)==value for key,value in case["expected_fields"].items())
                            checks["prepare_tool"] = any(e["kind"] == "tool_result" and e["payload"].get("name") == "prepare_ticket" and e["payload"].get("success") for e in events)
                            if checks["run_status"] and checks["workflow"]:
                                ticket = await request("learner", "POST", f"/runs/{run['id']}/confirm", {"approval": run["approval"]})
                                same = await request("learner", "POST", f"/runs/{run['id']}/confirm", {"approval": run["approval"]})
                                checks["idempotent_confirmation"] = ticket["id"] == same["id"]
                                lifecycle = await request("learner", "GET", "/service-cases/"+ticket["id"])
                                actions = ["approve", "provision"] if case["workflow"] == "access_request" else ["triage", "assign", "start", "resolve", "reopen", "resolve", "accept"]
                                for step, action in enumerate(actions):
                                    actor = "learner" if action in {"reopen", "accept"} else "support"
                                    lifecycle = await request(actor, "POST", f"/service-cases/{ticket['id']}/actions", {"action": action, "version": lifecycle["version"], "request_key": f"quality-{step}", "note": "合成评测操作："+action, "assignee": "support" if action == "assign" else ""})
                                expected = "fulfilled" if case["workflow"] == "access_request" else "closed"
                                checks["business_final_state"] = lifecycle["status"] == expected
                                context = await request("learner", "GET", "/service-catalog")
                                if expected == "fulfilled":
                                    grants = [g for g in context["my_grants"] if g["resource"]==case["expected_fields"]["resource_id"]]
                                    checks["persisted_business_effect"] = len(grants)==1 and grants[0]["level"]==case["expected_fields"]["access_level"] and abs((grants[0]["expires"]-time.time())-case["expected_fields"]["duration_days"]*86400)<60
                                else:
                                    checks["persisted_business_effect"] = any(a["id"]==case["expected_fields"]["asset_id"] and a["status"]=="available" for a in context["my_assets"])
                        scores = await judge.judge("\n".join(case["turns"]), run["answer"], encode({"reference": case["reference"], "turn_states": turn_states})) if use_judge else None
                        if scores:
                            detail["metadata"]["judge_failed"] = scores.judge_failed
                            detail["judge"] = asdict(scores)
                            if not scores.judge_failed: detail["scores"] = {k: getattr(scores, k) for k in ("relevance", "accuracy", "completeness", "helpfulness")}
                        detail.update({"run_id": run["id"], "answer": run["answer"], "turn_states": turn_states, "checks": checks,
                                       "routing": routing, "lifecycle": lifecycle, "draft": run.get("draft"), "evidence_documents": sorted(evidence_documents), "cited_documents": sorted(cited_documents),
                                       "passed": all(checks.values()) and (not use_judge or (scores.overall is not None and scores.overall >= .75))})
                    except asyncio.CancelledError: raise
                    except Exception as exc:
                        detail["metadata"]["execution_error"] = type(exc).__name__
                    details.append(detail)
                    progress(index+1, len(cases))
        finally:
            await agent.close()
        valid = [d for d in details if not d["metadata"].get("judge_failed") and not d["metadata"].get("execution_error")]
        scored = [d for d in valid if d["scores"]]
        intent = intent_report(corpus["intent_cases"])
        average = {k: statistics.mean(d["scores"][k] for d in scored) for k in ("relevance", "accuracy", "completeness", "helpfulness")} if scored else {}
        average.update(intent_accuracy=intent["accuracy"], intent_macro_f1=intent["macro_f1"], business_check_rate=sum(all(d.get("checks", {"missing": False}).values()) for d in details)/len(details))
        report = {"kind": "active_service_quality", "suite_id": suite_id, "corpus_version": corpus["version"], "provenance": corpus["provenance"],
                  "status": "complete" if len(valid) == len(details) else "partial", "total": len(details), "valid_total": len(valid),
                  "passed": sum(d["passed"] for d in details), "pass_rate": sum(d["passed"] for d in valid)/len(valid) if valid else 0,
                  "evaluation_errors": len(details)-len(valid), "avg_scores": average, "intent": intent, "results": details,
                  "regressions": [], "regression_note": "需人工选定相同用例、知识版本与评分配置的有效基线。",
                  "recommendations": ["合成开发集不能证明生产效果；同一 Flash 同时生成和评分存在评判偏差。", "业务状态变化来自隔离数据库中的真实事务；审批步骤由评测脚本模拟 IT 操作员。"],
                  "judge_enabled": use_judge, "sandbox": str(scratch.relative_to(ROOT))}
        return report
