"""Paid Flash + real PG/Redis/Chroma/E5/MCP against isolated current ASGI APIs.

Synthetic policies and scenarios only. Shares the original cumulative billing
ledger. No real business approvals, no source database copies, no external IM.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from resolvedesk.store import Store,Principal


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--live",action="store_true",help="Spend from the existing authorized Flash budget")
    parser.add_argument("--tools-only",action="store_true",help="Verify autonomous RAG/MCP/native use by two experts without repeating the dialog suite")
    args=parser.parse_args()
    if not args.live:parser.error("Use --live for explicitly budgeted real model verification")
    from dotenv import load_dotenv
    import httpx
    from sqlalchemy import text
    from core.paid_api import BudgetedAnthropic
    from resolvedesk.settings import ServiceSettings
    from resolvedesk.retrieval import LocalEmbedding,HybridKnowledge
    from resolvedesk.vectors import ChromaVectors
    from resolvedesk.coordination import Coordination
    from resolvedesk.services import DeskServices
    from resolvedesk.agent import DeskAgent
    from resolvedesk.app import create_app
    from resolvedesk.business import BusinessService
    load_dotenv(ROOT/".env",override=False)
    settings=ServiceSettings.load()
    identity=uuid.uuid4().hex
    schema="verify_it_"+identity
    collection="verify-it-"+identity
    store=Store(settings.database_url,schema=schema)
    embedding=LocalEmbedding(ROOT/"data/models/multilingual-e5-small")
    vectors=ChromaVectors(settings.chroma_url,embedding.identity,collection=collection)
    cache=Coordination(settings.redis_url,"verify-it-"+identity)
    kb=HybridKnowledge(store,embedding,vectors)
    support=Principal("demo","support","support");learner=Principal("demo","learner")
    for path in sorted((ROOT/"knowledge").glob("*.md")):
        content=path.read_text(encoding="utf-8")
        kb.add(support,path.stem,content.splitlines()[0].lstrip("# "),content)
    BusinessService(store).seed_demo()
    model=BudgetedAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY",""),base_url="https://api.deepseek.com/anthropic",timeout=25)
    before=model.ledger().summary()
    services=DeskServices(store,kb,model);agent=DeskAgent(store,kb,model,services,cache)
    token=secrets.token_urlsafe(36)
    api=create_app(store,kb,agent,{hashlib.sha256(token.encode()).hexdigest():vars(learner)})
    details=[]
    async def request(http,method,path,data=None):
        result=await http.request(method,path,json=data)
        result.raise_for_status();return result.json()
    async def wait(http,run):
        async with asyncio.timeout(120):
            while True:
                turn=await request(http,"GET",f"/runs/{run['id']}/turns/{run['turn_key']}")
                if turn["status"]not in {"running","queued"}:return turn
                await asyncio.sleep(.2)
    async def examine(http,case,run):
        final=await wait(http,run)
        events=store.events(learner,run["id"])
        route=next(e["payload"] for e in reversed(events) if e["kind"]=="routing")
        outcome=next((e["payload"] for e in reversed(events) if e["kind"]=="execution_outcome"),None)
        expected=case["domain"]
        checks={"domain":route["intent"]==expected,"method":route["intent_method"]=="it_three_way_fusion_v1",
            "real_sources":route["source_scores"]["llm"]["available"] and route["source_scores"]["embedding"]["available"],
            "status":final["status"]in case["statuses"],"no_business_write":not store.list_tickets(learner)}
        if case.get("citation"):checks["citation"]="[C-" in final["answer"]
        if case.get("roles"):checks["actual_roles"]=bool(outcome) and set(outcome["actual_roles"])==set(case["roles"])
        if case.get("critical"):
            checks["critical"]=route["urgency"]=="CRITICAL" and bool(outcome) and outcome["escalated"]
            checks["immediate_guidance"]="停止使用" in final["answer"] and "断开充电" in final["answer"] and "尚未连接真实人工队列" in final["answer"] and "[C-" in final["answer"]
        if case.get("draft_days"):checks["draft_days"]=bool(final["draft"]) and final["draft"]["duration_days"]==case["draft_days"]
        if case.get("device_reset"):checks["device_reset"]=bool(final["draft"]) and final["draft"]["workflow"]=="device_repair" and not final["draft"]["resource_id"] and final["draft"]["duration_days"]==0
        result={"case":case["id"],"passed":all(checks.values()),"checks":checks,"status":final["status"],"error":final["error"],
            "route":route,"outcome":outcome,"answer":final["answer"],"draft":final["draft"],"simulation":True,
            "specialists":[e["payload"] for e in events if e["kind"]=="specialist_result" and e["seq"]>final["event_cursor"]]}
        details.append(result)
        print(json.dumps({"case":case["id"],"passed":result["passed"],"status":final["status"]},ensure_ascii=False),flush=True)
        return final
    try:
        await services.start()
        async with api.router.lifespan_context(api),httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url="http://verify-it.local",headers={"Authorization":"Bearer "+token},timeout=130) as http:
            if args.tools_only:
                message="VPN连接超时且IDEA Ultimate授权席位需要申请。请两个专业节点分别查询当前知识库，并调用标准MCP诊断清单（网络/软件）；软件节点同时核验IDEA目录。完成后简要解释制度，不提交或修改任何业务。"
                run,_=store.create_run(learner,message,"verify-tools-"+uuid.uuid4().hex)
                store.claim(learner,run["id"])
                routing=await services.route(learner,run["id"],message,[],{})
                result=await services.consult(learner,run["id"],message,[],{"evidence":[],"business":{"simulation":True}},routing)
                for opinion in result["specialist_opinions"]:
                    checks={"status":opinion["status"]=="success","rag":"search_knowledge_base"in opinion["tools_used"],
                        "mcp":"mcp_support_support_checklist"in opinion["tools_used"],"no_write":not store.list_tickets(learner)}
                    if opinion["role"]=="billing":checks["it_native"]="check_license_request"in opinion["tools_used"]
                    details.append({"case":"autonomous-"+opinion["requested_role"],"passed":all(checks.values()),"checks":checks,"opinion":opinion,"simulation":True})
                print(json.dumps({"autonomous_experts":len(details),"passed":sum(r["passed"] for r in details)},ensure_ascii=False),flush=True)
                return
            cases=[
                {"id":"network","message":"Windows 11的VPN提示错误691，普通网页正常。请依据知识库给出排查步骤，不要创建工单。","domain":"network_endpoint","statuses":["completed"],"roles":["technical"],"citation":True},
                {"id":"licensing","message":"IntelliJ IDEA Ultimate商业席位如何申请、许可证到期怎么回收？只解释制度，不提交申请。","domain":"software_license","statuses":["completed"],"roles":["billing"],"citation":True},
                {"id":"composite","message":"VPN持续连接超时，普通网络正常；同时IDEA Ultimate许可证到期。请分别给出初步排查和授权申请制度，只读解释，不要建单。","domain":"network_endpoint","statuses":["completed"],"roles":["technical","billing"],"citation":True},
                {"id":"critical","message":"我的LT-1001笔记本电池已经鼓包，是否还能继续开机？请按安全制度解释并说明人工处理边界，不要建单。","domain":"device_service","statuses":["completed","awaiting_input"],"critical":True},
                {"id":"unknown","message":"我有个事情想问一下，还没想好具体怎么说。","domain":"general","statuses":["awaiting_input"]},
            ]
            for case in cases:
                run=await request(http,"POST","/runs",{"message":case["message"],"request_key":"verify-"+uuid.uuid4().hex})
                await examine(http,case,run)
            permission={"id":"permission-draft","domain":"account_access","statuses":["awaiting_confirmation"],"draft_days":7}
            run=await request(http,"POST","/runs",{"message":"请准备权限申请草稿：summary仓库阅读申请，description阅读项目源码完成学习，category access，priority normal，workflow access_request，resource_id repo-resolvedesk，access_level read，duration_days 7。仅准备草稿，等我确认，不要声称已开通。","request_key":"verify-"+uuid.uuid4().hex})
            await examine(http,permission,run)
            reply=await request(http,"POST",f"/runs/{run['id']}/reply",{"message":"有效期改成14天，仍为read，其他信息不变。请更新当前权限申请草稿。","request_key":"verify-"+uuid.uuid4().hex})
            await examine(http,{"id":"permission-correction","domain":"account_access","statuses":["awaiting_confirmation"],"draft_days":14},reply)
            switched=await request(http,"POST",f"/runs/{run['id']}/reply",{"message":"换个问题，先不处理权限申请。请准备设备维修草稿：summary键盘维修，description键盘失灵且外接键盘正常，category hardware，priority normal，workflow device_repair，asset_id LT-1001。仅给我草稿。","request_key":"verify-"+uuid.uuid4().hex})
            await examine(http,{"id":"device-switch","domain":"device_service","statuses":["awaiting_confirmation"],"device_reset":True},switched)
    finally:
        await agent.close();await cache.close()
        after=model.ledger().summary()
        report={"kind":"current_asgi_real_it_migration","simulation":True,"schema":schema,"collection":collection,
            "passed":sum(r["passed"] for r in details),"total":len(details),"results":details,"budget_before":before,"budget_after":after,
            "dependencies":"real PostgreSQL Redis Chroma pinned local E5 stdio MCP; only Flash uses paid API"}
        path=ROOT/"reports"/("it-migration-live-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")+".json")
        with path.open("x",encoding="utf-8") as output:json.dump(report,output,ensure_ascii=False,indent=2)
        print(json.dumps({"evidence":str(path.relative_to(ROOT)),"passed":report["passed"],"total":report["total"]},ensure_ascii=False),flush=True)
        # This process owns only its fresh synthetic schema and Chroma collection.
        from tools.migrate_services import TABLES
        with store.database.engine.begin() as db:
            tables=set(db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname=:schema"),{"schema":schema}).scalars())
            if not schema.startswith("verify_it_") or not tables<=TABLES:raise RuntimeError("Unexpected verification scope; preserving it")
            db.exec_driver_sql('DROP SCHEMA "'+schema+'" CASCADE')
        if vectors.name!=collection or not collection.startswith("verify-it-"):raise RuntimeError("Unexpected vector scope")
        vectors.request("DELETE",vectors.base+"/"+collection);vectors.close();store.close()
    if len(details)!=8 or not all(r["passed"] for r in details):raise SystemExit(1)


if __name__=="__main__":asyncio.run(main())
