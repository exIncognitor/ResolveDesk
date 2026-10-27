"""Local API: python -X utf8 -m uvicorn resolvedesk.app:app --port 8080."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from resolvedesk.agent import DeskAgent
from resolvedesk.retrieval import HybridKnowledge, LocalEmbedding
from resolvedesk.store import Conflict, ReceiptExpired, Principal, Store, encode

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"completed", "failed", "paused", "awaiting_input", "awaiting_confirmation"}


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NewRun(Input):
    message: str = Field(min_length=1, max_length=4000)
    request_key: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


class Reply(Input):
    message: str = Field(min_length=1, max_length=4000)
    request_key: str = Field(min_length=8,max_length=100,pattern=r"^[a-zA-Z0-9_-]+$")


class Approval(Input):
    approval: str = Field(pattern=r"^[a-f0-9]{64}$")


class Search(Input):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    mode: Literal["hybrid", "bm25", "vector"] = "hybrid"


class Document(Input):
    document_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=100000)
    visibility: Literal["tenant", "private"] = "tenant"


class Feedback(Input):
    rating: Literal["good", "bad"]
    note: str = Field(default="", max_length=2000)


class Regression(Input):
    document: str = Field(min_length=1,max_length=100)
    section: str = Field(min_length=1,max_length=200)


class ServiceAction(Input):
    action: Literal["approve","reject","provision","cancel","triage","assign","start","resolve","accept","reopen"]
    version: int = Field(ge=1)
    request_key: str = Field(min_length=8,max_length=100,pattern=r"^[a-zA-Z0-9_-]+$")
    note: str = Field(min_length=1,max_length=2000)
    assignee: str = Field(default="",max_length=100)


class EvaluationOptions(Input):
    kind: Literal["retrieval","quality"] = "retrieval"
    limit: int = Field(default=6,ge=1,le=6)
    use_judge: bool = True


def create_app(store=None, knowledge=None, agent=None, accounts=None):
    injected = store is not None

    @asynccontextmanager
    async def lifespan(app):
        coordination = None
        vectors = None
        if not injected:
            load_dotenv(ROOT / ".env", override=False)
        try:
            if not injected:
                from resolvedesk.settings import ServiceSettings
                from resolvedesk.coordination import Coordination
                from resolvedesk.vectors import ChromaVectors
                settings = ServiceSettings.load()
                app.state.store = Store(settings.database_url)
                embedding = LocalEmbedding(ROOT / "data" / "models" / "multilingual-e5-small")
                app.state.embedding_error = None
                vectors = await asyncio.to_thread(ChromaVectors,settings.chroma_url,embedding.identity)
                coordination = Coordination(settings.redis_url,settings.namespace)
                await coordination.redis.ping()
                app.state.knowledge = HybridKnowledge(app.state.store,embedding,vectors)
                app.state.agent = DeskAgent(app.state.store,app.state.knowledge,coordination=coordination)
                from resolvedesk.services import DeskServices
                app.state.agent.services = DeskServices(app.state.store,app.state.knowledge,app.state.agent.client)
                await app.state.agent.services.start()
                app.state.accounts = json.loads((ROOT / "data" / "auth.json").read_text(encoding="utf-8"))
            from resolvedesk.evaluations import EvaluationService
            app.state.evaluations = EvaluationService(app.state.store,app.state.knowledge)
            app.state.store.recover_expired()
            app.state.agent.monitor.start()
            yield
        finally:
            if hasattr(app.state,"evaluations"):
                await app.state.evaluations.close()
            if hasattr(app.state, "agent"):
                await app.state.agent.close()
            if coordination: await coordination.close()
            if vectors: vectors.close()
            if not injected and hasattr(app.state,"store"): app.state.store.close()

    app = FastAPI(title="ResolveDesk", version="0.1.0", lifespan=lifespan)
    if injected:
        app.state.store, app.state.knowledge, app.state.agent = store, knowledge, agent
        app.state.accounts = accounts or {}
        app.state.embedding_error = None

    def principal(request: Request, authorization: str = Header(default="")):
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer token required")
        token = authorization[7:]
        key = hashlib.sha256(token.encode()).hexdigest()
        account = request.app.state.accounts.get(key)
        if account is None or account.get("role") not in {"user", "support"}:
            raise HTTPException(401, "Invalid credentials")
        return Principal(account["tenant"], account["user"], account["role"])

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"detail": "Resource not found"})

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ReceiptExpired)
    async def expired_receipt(request,exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=410,content={"detail":str(exc)})

    @app.exception_handler(PermissionError)
    async def forbidden(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=403, content={"detail": "Support role required"})

    @app.exception_handler(ValueError)
    async def invalid_input(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=422, content={"detail": "Invalid input"})

    @app.get("/", include_in_schema=False)
    def index():
        frontend = ROOT/"frontend"/"dist"/"index.html"
        return FileResponse(frontend if frontend.exists() else ROOT/"resolvedesk"/"index.html")

    if (ROOT/"frontend"/"dist"/"assets").exists():
        app.mount("/assets",StaticFiles(directory=ROOT/"frontend"/"dist"/"assets"),name="frontend-assets")

    @app.get("/runtime-config.js",include_in_schema=False)
    def runtime_config():
        from fastapi import Response
        return Response("window.__RESOLVEDESK_CONFIG__={pythonApiUrl:''};",media_type="application/javascript")

    @app.get("/health")
    def health(request: Request):
        return {"status": "ok", "service": "ResolveDesk", "model": "deepseek-flash",
                "database":request.app.state.store.backend,
                "vector_backend":"chroma" if request.app.state.knowledge.vector_index else "offline-fixture",
                "coordination":"redis" if request.app.state.agent.coordination else "offline-fixture",
                "semantic_retrieval": request.app.state.knowledge.embedding is not None,
                "paid_api_enabled": os.getenv("RESOLVEDESK_PAID_API_ENABLED", "false").lower() == "true"}

    @app.get("/ready")
    async def ready(request:Request):
        from fastapi.responses import JSONResponse
        state=request.app.state
        checks={}
        def database():
            with state.store.db() as db: return db.execute("SELECT 1").fetchone()[0]==1
        try: checks["database"]=await asyncio.to_thread(database)
        except Exception: checks["database"]=False
        if state.agent.coordination:
            try: checks["redis"]=bool(await state.agent.coordination.redis.ping())
            except Exception: checks["redis"]=False
        if state.knowledge.vector_index:
            try:
                checks["chroma"]=bool(await asyncio.to_thread(state.knowledge.vector_index.request,"GET","/api/v2/heartbeat"))
            except Exception: checks["chroma"]=False
        ok=all(checks.values())
        return JSONResponse(status_code=200 if ok else 503,content={"ready":ok,"components":checks})

    @app.get("/me")
    def me(p=Depends(principal)):
        return vars(p)

    @app.get("/system")
    def system(request: Request, p=Depends(principal)):
        if p.role != "support":
            raise PermissionError()
        return {"retrieval": request.app.state.knowledge.status(), "embedding_error": request.app.state.embedding_error,
                "budget": request.app.state.agent.client.ledger().summary(),
                "capabilities":{"skills":bool(request.app.state.agent.services),"memory":bool(request.app.state.agent.services),
                    "routing":bool(request.app.state.agent.services),"streaming":True,"step_checkpoints":True}}

    @app.post("/runs", status_code=202)
    async def create_run(data: NewRun, request: Request, p=Depends(principal)):
        if not data.message.strip():
            raise HTTPException(422, "Message cannot be blank")
        if len(request.app.state.agent.tasks) >= 32:
            raise HTTPException(503, "Local queue is full")
        run, created = request.app.state.store.create_run(p, data.message, data.request_key)
        if created:
            request.app.state.agent.schedule(p, run["id"])
        return {**run,"replayed":not created}

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, request: Request, p=Depends(principal)):
        return request.app.state.store.get_run(p, run_id)

    @app.get("/runs/{run_id}/turns/{request_key}")
    def get_turn(run_id:str,request_key:str,request:Request,p=Depends(principal)):
        return request.app.state.store.get_turn(p,run_id,request_key)

    @app.get("/runs")
    def list_runs(request:Request,p=Depends(principal)):
        return request.app.state.store.list_runs(p)

    @app.get("/runs/{run_id}/messages")
    def run_messages(run_id:str,request:Request,p=Depends(principal)):
        return request.app.state.store.history(p,run_id)

    @app.post("/runs/{run_id}/reply", status_code=202)
    async def reply(run_id: str, data: Reply, request: Request, p=Depends(principal)):
        if not data.message.strip():
            raise HTTPException(422, "Message cannot be blank")
        turn,created = request.app.state.store.reply(p,run_id,data.message,data.request_key)
        if created: request.app.state.agent.schedule(p,run_id)
        return {**turn,"replayed":not created}

    @app.post("/runs/{run_id}/confirm")
    async def confirm(run_id: str, data: Approval, request: Request, p=Depends(principal)):
        ticket = request.app.state.store.confirm(p,run_id,data.approval)
        if request.app.state.agent.services:
            try:
                await request.app.state.agent.services.memory.remember(p,run_id)
            except Exception as exc:
                with request.app.state.store.db(True) as db:
                    request.app.state.store._event(db,run_id,"memory_error",{"type":type(exc).__name__})
        return ticket

    @app.post("/runs/{run_id}/pause")
    async def pause(run_id: str, request: Request, p=Depends(principal)):
        await request.app.state.agent.pause(p, run_id)
        return request.app.state.store.get_run(p, run_id)

    @app.post("/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str, request: Request, p=Depends(principal)):
        request.app.state.store.resume(p, run_id)
        request.app.state.agent.schedule(p, run_id)
        return request.app.state.store.get_run(p, run_id)

    @app.get("/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = 0, follow: bool = True, turn_key: str | None = None,
                     last_event_id: str = Header(default=""), p=Depends(principal)):
        request.app.state.store.get_run(p, run_id)
        if turn_key: request.app.state.store.get_turn(p,run_id,turn_key)
        try:
            cursor = max(0, after, int(last_event_id or 0))
        except ValueError:
            raise HTTPException(422, "Invalid event cursor")
        async def generate():
            nonlocal cursor
            ticks = 0
            while not await request.is_disconnected():
                turn = request.app.state.store.get_turn(p,run_id,turn_key) if turn_key else request.app.state.store.get_run(p,run_id)
                terminal = turn["status"] in TERMINAL
                cursor = max(cursor,turn["event_cursor"]) if turn_key else cursor
                batch = request.app.state.store.events(p, run_id, cursor)
                if turn_key and terminal: batch = [event for event in batch if event["seq"] <= turn["event_end"]]
                for event in batch:
                    cursor = event["seq"]
                    yield "id: " + str(cursor) + "\nevent: " + event["kind"] + "\ndata: " + encode(event["payload"]) + "\n\n"
                if len(batch) < 200 and (not follow or terminal):
                    break
                ticks += 1
                if ticks % 50 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.2)
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/knowledge/search")
    async def search(data: Search, request: Request, p=Depends(principal)):
        return await asyncio.to_thread(request.app.state.knowledge.search, p, data.query, data.top_k, data.mode)

    @app.post("/knowledge/documents")
    async def add_document(data: Document, request: Request, p=Depends(principal)):
        if p.role != "support":
            raise PermissionError()
        return await asyncio.to_thread(request.app.state.knowledge.add, p, data.document_id, data.title, data.content, data.visibility)

    @app.get("/knowledge/stats")
    def knowledge_stats(request:Request,p=Depends(principal)):
        with request.app.state.store.db() as db:
            count = db.execute("SELECT COUNT(*) FROM chunks WHERE tenant=? AND (visibility='tenant' OR owner=?)",(p.tenant,p.user)).fetchone()[0]
        return {"total_chunks":count}

    @app.get("/skills")
    def skills(request:Request,p=Depends(principal)):
        services = request.app.state.agent.services
        return services.skills.summary() if services else {"skills":[],"count":0,"errors":[]}

    @app.post("/skills/reload")
    def reload_skills(request:Request,p=Depends(principal)):
        if p.role!="support":raise PermissionError()
        request.app.state.agent.services.skills.reload()
        return request.app.state.agent.services.skills.summary()

    @app.get("/memory")
    def memory(request:Request,p=Depends(principal)):
        services = request.app.state.agent.services
        return services.memory.inspect(p) if services else {"profile":{},"episodes":0,"summaries":0}

    @app.get("/mcp")
    def mcp(request:Request,p=Depends(principal)):
        services = request.app.state.agent.services
        return services.mcp.summary() if services else {"servers":[]}

    @app.get("/monitor")
    def monitor(request:Request,p=Depends(principal)):
        if p.role=="support":return request.app.state.agent.monitor.snapshot(p)
        with request.app.state.store.db() as db:
            count = db.execute("SELECT COUNT(*) FROM runs WHERE tenant=? AND owner=?",(p.tenant,p.user)).fetchone()[0]
        stats = {}
        with request.app.state.store.db() as db:
            routes = db.execute("SELECT e.payload,r.status FROM events e JOIN runs r ON e.run_id=r.id WHERE e.kind='routing' AND r.tenant=? AND r.owner=?",(p.tenant,p.user))
            for row in routes:
                role=json.loads(row['payload']).get('primary','general')
                entry=stats.setdefault(role,{'total':0,'success':0})
                entry['total']+=1
                entry['success']+=row['status'] in {'completed','awaiting_input','awaiting_confirmation'}
        for entry in stats.values():entry['success_rate']=entry['success']/entry['total']
        return {"agent_stats":stats,"tool_stats":{},"active_alerts":[],"suggestions":[],"total_requests":count}

    @app.get("/metrics")
    def metrics(request:Request,p=Depends(principal)):
        if p.role!="support":raise PermissionError()
        from fastapi import Response
        return Response(request.app.state.agent.monitor.prometheus(p),media_type="text/plain; version=0.0.4")

    @app.get("/trace/tool/{run_id}")
    def trace(run_id:str,request:Request,turn_key:str|None=None,p=Depends(principal)):
        current=request.app.state.store.get_turn(p,run_id,turn_key) if turn_key else request.app.state.store.get_run(p,run_id)
        events = request.app.state.store.events(p,run_id,current['event_cursor'])
        if current["status"]in TERMINAL:events=[event for event in events if event["seq"]<=current["event_end"]]
        calls = [dict(e["payload"],tool_name=e["payload"].get("name"),input={}) for e in events if e["kind"]=="tool_result"]
        return {"request_id":run_id,"found":True,"trace":{"tool_calls":calls,"tools_used":[c["tool_name"] for c in calls],"events":events}}

    @app.post("/eval/run",status_code=202)
    async def evaluation(request:Request,data:EvaluationOptions|None=None,p=Depends(principal)):
        options = data or EvaluationOptions()
        return request.app.state.evaluations.submit(p,**options.model_dump())

    @app.post("/eval/jobs/{job_id}/baseline")
    def evaluation_baseline(job_id:str,request:Request,p=Depends(principal)):
        return request.app.state.evaluations.set_baseline(p,job_id)

    @app.get("/eval/jobs/{job_id}")
    def evaluation_job(job_id:str,request:Request,p=Depends(principal)):
        return request.app.state.evaluations.get(p,job_id)

    @app.get("/eval/jobs")
    def evaluation_jobs(request:Request,p=Depends(principal)):
        return request.app.state.evaluations.list(p)

    @app.post("/eval/jobs/{job_id}/resume")
    async def resume_evaluation(job_id:str,request:Request,p=Depends(principal)):
        return request.app.state.evaluations.resume(p,job_id)

    @app.post("/evaluation/candidates/{run_id}/promote")
    def promote(run_id:str,data:Regression,request:Request,p=Depends(principal)):
        return request.app.state.evaluations.promote(p,run_id,data.document,data.section)

    @app.get("/knowledge/citations/{citation}")
    def citation(citation: str, request: Request, p=Depends(principal)):
        return request.app.state.knowledge.citation(p, citation)

    @app.get("/tickets")
    def tickets(request: Request, p=Depends(principal)):
        return request.app.state.store.list_tickets(p)

    @app.get("/service-catalog")
    def service_catalog(request:Request,p=Depends(principal)):
        return request.app.state.agent.business.context(p)

    @app.get("/service-cases")
    def service_cases(request:Request,p=Depends(principal)):
        return request.app.state.agent.business.list(p)

    @app.get("/service-cases/{ticket_id}")
    def service_case(ticket_id:str,request:Request,p=Depends(principal)):
        return request.app.state.agent.business.get(p,ticket_id)

    @app.post("/service-cases/{ticket_id}/actions")
    def service_action(ticket_id:str,data:ServiceAction,request:Request,p=Depends(principal)):
        return request.app.state.agent.business.transition(p,ticket_id,**data.model_dump())

    @app.get("/tickets/{ticket_id}")
    def ticket(ticket_id: str, request: Request, p=Depends(principal)):
        return request.app.state.store.get_ticket(p, ticket_id)

    @app.post("/runs/{run_id}/feedback")
    def feedback(run_id: str, data: Feedback, request: Request, p=Depends(principal)):
        request.app.state.store.feedback(p, run_id, data.rating, data.note)
        return {"saved": True}

    @app.get("/evaluation/candidates")
    def candidates(request: Request, p=Depends(principal)):
        return request.app.state.store.regression_candidates(p)

    return app


app = create_app()
