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
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from resolvedesk.agent import DeskAgent
from resolvedesk.retrieval import HybridKnowledge, LocalEmbedding
from resolvedesk.store import Conflict, ReceiptExpired, Principal, Store, encode
from resolvedesk.eval_models import EvalDataset,EvaluationOptions,ResumeEvaluation
from resolvedesk.knowledge_governance import KnowledgeMetadata

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
    content: str = Field(min_length=1, max_length=10*1024*1024)
    visibility: Literal["tenant", "private"] = "tenant"
    metadata:KnowledgeMetadata|None=None


class PublicationReview(Input):
    action:Literal['publish','withdraw']
    version:str=Field(min_length=1,max_length=64)
    revision:int=Field(ge=1)
    note:str=Field(min_length=1,max_length=2000)


class PublicationMetadata(Input):
    version:str=Field(min_length=1,max_length=64)
    revision:int=Field(ge=0)
    note:str=Field(min_length=1,max_length=2000)
    metadata:KnowledgeMetadata


class Feedback(Input):
    rating: Literal["good", "bad"]
    note: str = Field(default="", max_length=2000)


class Regression(Input):
    document: str = Field(min_length=1,max_length=100)
    section: str = Field(min_length=1,max_length=200)


class ServiceAction(Input):
    action: Literal["approve","reject","provision","cancel","triage","assign","start","resolve","accept","reopen","reserve","allocate","revoke"]
    version: int = Field(ge=1)
    request_key: str = Field(min_length=8,max_length=100,pattern=r"^[a-zA-Z0-9_-]+$")
    note: str = Field(min_length=1,max_length=2000)
    assignee: str = Field(default="",max_length=100)


class MCPDebugCall(Input):
    tool: str = Field(min_length=1,max_length=64,pattern=r"^[A-Za-z0-9_-]+$")
    arguments: dict = Field(default_factory=dict)
    role: Literal["general","technical","billing","escalation"] = "general"


def create_app(store=None, knowledge=None, agent=None, accounts=None, *, identity=None, models=None):
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
                from resolvedesk.identity import IdentityService
                from resolvedesk.model_admin import ModelRegistry,ManagedModelClient
                app.state.identity = IdentityService(app.state.store)
                app.state.models = ModelRegistry(app.state.store)
                client=ManagedModelClient(app.state.models)
                app.state.agent = DeskAgent(app.state.store,app.state.knowledge,client,coordination=coordination)
                app.state.agent.business.directory=app.state.identity
                from resolvedesk.services import DeskServices
                app.state.agent.services = DeskServices(app.state.store,app.state.knowledge,app.state.agent.client,coordination=coordination)
                await app.state.agent.services.start()
                app.state.accounts = json.loads((ROOT / "data" / "auth.json").read_text(encoding="utf-8"))
            from resolvedesk.evaluations import EvaluationService
            app.state.evaluations = EvaluationService(app.state.store,app.state.knowledge,
                client_factory=getattr(app.state,'models',None),coordination=app.state.agent.coordination)
            app.state.store.recover_expired()
            app.state.agent.monitor.start()
            from resolvedesk.knowledge_mcp import create_knowledge_mcp
            server,protocol=create_knowledge_mcp(app)
            app.state.knowledge_mcp=server
            app.state.knowledge_mcp_http=protocol
            async with server.session_manager.run():
                yield
        finally:
            app.state.knowledge_mcp_http=None
            if hasattr(app.state,'import_tasks'):
                await asyncio.gather(*list(app.state.import_tasks),return_exceptions=True)
            if hasattr(app.state,"evaluations"):
                await app.state.evaluations.close()
            if hasattr(app.state, "agent"):
                await app.state.agent.close()
            if coordination: await coordination.close()
            if vectors: vectors.close()
            if not injected and hasattr(app.state,"store"): app.state.store.close()

    app = FastAPI(title="ResolveDesk", version="0.1.0", lifespan=lifespan)
    class KnowledgeProtocolEndpoint:
        async def __call__(self,scope,receive,send):
            protocol=getattr(app.state,'knowledge_mcp_http',None)
            if protocol is None:
                from starlette.responses import JSONResponse
                return await JSONResponse({'detail':'MCP protocol not ready'},status_code=503)(scope,receive,send)
            return await protocol(scope,receive,send)
    app.mount('/mcp/knowledge',KnowledgeProtocolEndpoint(),name='knowledge-mcp')
    if injected:
        app.state.store, app.state.knowledge, app.state.agent = store, knowledge, agent
        app.state.accounts = accounts or {}
        app.state.embedding_error = None
        app.state.identity,app.state.models = identity,models

    def principal(request: Request, authorization: str = Header(default="")):
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer token required")
        token = authorization[7:]
        from resolvedesk.identity import resolve_identity
        account = resolve_identity(request.app.state,token)
        if account is None:
            raise HTTPException(401, "Invalid credentials")
        return account

    from resolvedesk.admin_api import install_admin
    install_admin(app,principal)

    from fastapi.exceptions import RequestValidationError
    @app.exception_handler(RequestValidationError)
    async def invalid_request(request,exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=422,content={'detail':[{'loc':list(e['loc']),'msg':e['msg'],'type':e['type']} for e in exc.errors()]})

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
        return {"status": "ok", "service": "ResolveDesk", "model": getattr(request.app.state.agent.client,'model','deepseek-flash'),
                "database":request.app.state.store.backend,
                "vector_backend":"chroma" if request.app.state.knowledge.vector_index else "offline-fixture",
                "coordination":"redis" if request.app.state.agent.coordination else "offline-fixture",
                "semantic_retrieval": request.app.state.knowledge.embedding is not None}

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
        services=request.app.state.agent.services
        intent=services.roles._intent_recognizer if services else None
        retrieval = request.app.state.knowledge.status()
        retrieval['chunks'] = request.app.state.knowledge.stats(p)['total_chunks']
        return {"retrieval": retrieval, "embedding_error": request.app.state.embedding_error,
                "model": request.app.state.models.describe(p) if getattr(request.app.state,'models',None) else getattr(request.app.state.agent.client,'describe',lambda:{})(),
                "usage": getattr(request.app.state.agent.client,'usage_summary',lambda:{})(),
                "intent":{"implementation":type(intent).__name__,"vector_provider":getattr(getattr(intent,"embedding",None),"identity",None)},
                "specialists":{role.value:{"implementations":[type(agent).__name__ for agent in pool],"tools":list(pool[0].get_tools())} for role,pool in services.roles._pool.items()} if services else {},
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

    @app.get("/runs/{run_id}/state")
    def conversation_state(run_id:str,request:Request,p=Depends(principal)):
        request.app.state.store.get_run(p,run_id)
        services=request.app.state.agent.services
        return services.entities.get(p,run_id) if services else {"domain":"general","entities":{},"sources":{},"task_epoch":0}

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
                turn = await asyncio.to_thread(request.app.state.store.get_turn,p,run_id,turn_key) if turn_key else await asyncio.to_thread(request.app.state.store.get_run,p,run_id)
                terminal = turn["status"] in TERMINAL
                cursor = max(cursor,turn["event_cursor"]) if turn_key else cursor
                batch = await asyncio.to_thread(request.app.state.store.events,p,run_id,cursor)
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
        services=request.app.state.agent.services
        if services:return await services.search(p,data.query,data.top_k,optimize=False,mode=data.mode)
        return await asyncio.to_thread(request.app.state.knowledge.search, p, data.query, data.top_k, data.mode)

    @app.post("/knowledge/documents",openapi_extra={'requestBody':{'required':True,'content':{'application/json':{'schema':Document.model_json_schema()}}}})
    async def add_document(request:Request,p=Depends(principal)):
        result=await import_documents(request,p,False,single=True)
        return result['results'][0]

    async def import_documents(request,p,upload,single=False):
        if p.role!='support':raise PermissionError()
        from resolvedesk.imports import read_batch,read_upload
        # Keep CPU/embedding work bounded across HTTP requests in this worker.
        if not hasattr(request.app.state,'import_slots'):request.app.state.import_slots=asyncio.Semaphore(2)
        slots=request.app.state.import_slots
        if slots.locked():raise HTTPException(429,'Knowledge imports are busy; retry after the current request finishes')
        await slots.acquire()
        try:
            if single:
                from resolvedesk.imports import bounded_body,json_value,MAX_IMPORT_BYTES
                if request.headers.get('content-type','').split(';')[0].strip().lower()!='application/json':raise HTTPException(415,'Use application/json')
                data=Document.model_validate(json_value(await bounded_body(request,MAX_IMPORT_BYTES)))
                docs=[data.model_dump()]
            else:docs=await (read_upload(request,p) if upload else read_batch(request,p))
            task=asyncio.create_task(asyncio.to_thread(request.app.state.knowledge.add_many,p,docs))
        except BaseException:slots.release();raise
        if not hasattr(request.app.state,'import_tasks'):request.app.state.import_tasks=set()
        request.app.state.import_tasks.add(task)
        def imported(done):
            slots.release()
            request.app.state.import_tasks.discard(done)
            if not done.cancelled():done.exception()
        task.add_done_callback(imported)
        # Cancellation cannot stop an SQL/embedding thread; keep its slot until
        # actual completion and never claim that disconnected imports rolled back.
        return await asyncio.shield(task)

    from resolvedesk.imports import ImportDocument
    batch_schema={'type':'object','required':['documents'],'additionalProperties':False,'properties':{'documents':{'type':'array','minItems':1,'maxItems':100,'items':ImportDocument.model_json_schema()}}}
    @app.post('/knowledge/add',openapi_extra={'requestBody':{'required':True,'content':{'application/json':{'schema':batch_schema}}}})
    @app.post('/knowledge/batch',openapi_extra={'requestBody':{'required':True,'content':{'application/json':{'schema':batch_schema}}}})
    async def batch_knowledge(request:Request,p=Depends(principal)):
        return await import_documents(request,p,False)

    @app.post('/knowledge/upload',openapi_extra={'requestBody':{'required':True,'content':{'multipart/form-data':{'schema':{'type':'object','required':['file'],'properties':{'file':{'type':'string','format':'binary'}}}}}}})
    async def upload_knowledge(request:Request,p=Depends(principal)):
        return await import_documents(request,p,True)

    @app.get("/knowledge/stats")
    def knowledge_stats(request:Request,p=Depends(principal)):
        return request.app.state.knowledge.stats(p)

    @app.get('/knowledge/publications')
    def publications(request:Request,p=Depends(principal)):
        return request.app.state.knowledge.publications(p)

    @app.get('/knowledge/publications/{document_id}/audit')
    def publication_audit(document_id:str,request:Request,p=Depends(principal)):
        return request.app.state.knowledge.governance.audit_list(p,document_id)

    @app.post('/knowledge/publications/{document_id}/review')
    def publication_review(document_id:str,data:PublicationReview,request:Request,p=Depends(principal)):
        return request.app.state.knowledge.publication_action(p,document_id,data.action,data.version,data.revision,data.note)

    @app.put('/knowledge/publications/{document_id}/metadata')
    def publication_metadata(document_id:str,data:PublicationMetadata,request:Request,p=Depends(principal)):
        return request.app.state.knowledge.publication_action(p,document_id,'metadata',data.version,data.revision,data.note,data.metadata.model_dump())

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
        if p.role!='support':raise PermissionError()
        services = request.app.state.agent.services
        return services.mcp.summary() if services else {"servers":[]}

    @app.get("/integrations/mcp")
    def mcp_integration(request:Request,p=Depends(principal)):
        if p.role!='support':raise PermissionError()
        services=request.app.state.agent.services
        return {"protocol_endpoint":"/mcp/knowledge/","read_only":True,
            **(services.mcp.summary() if services else {"servers":[]})}

    @app.post("/integrations/mcp/call")
    async def mcp_debug(data:MCPDebugCall,request:Request,p=Depends(principal)):
        if p.role!='support':raise PermissionError()
        if len(encode(data.arguments).encode())>16000:raise HTTPException(413,"MCP arguments too large")
        services=request.app.state.agent.services
        if services is None:raise HTTPException(503,"MCP not configured")
        try:return await services.call_external(p,data.tool,data.arguments,data.role)
        except PermissionError:raise
        except Exception:raise HTTPException(502,"Read-only MCP call failed") from None

    @app.get("/integrations/embedding")
    @app.get("/integrations/intent-embedding")
    def intent_embedding(request:Request,p=Depends(principal)):
        if p.role!='support':raise PermissionError()
        services=request.app.state.agent.services
        if services is None:return {"configured":False,"state":"not_configured"}
        return services.roles._intent_recognizer.embedding_status()

    @app.get("/monitor")
    def monitor(request:Request,p=Depends(principal)):
        if p.role=='support' and request.app.state.agent.services:request.app.state.agent.services.publish_tool_health(p.tenant)
        return request.app.state.agent.monitor.snapshot(p)

    @app.get("/metrics")
    def metrics(request:Request,p=Depends(principal)):
        if p.role!="support":raise PermissionError()
        from fastapi import Response
        return Response(request.app.state.agent.monitor.prometheus(p),media_type="text/plain; version=0.0.4")

    @app.get('/trace/tools')
    def recent_trace_list(request:Request,before:int|None=Query(default=None,ge=1),limit:int=Query(default=20,ge=1,le=100),p=Depends(principal)):
        from resolvedesk.trace_queries import recent_traces
        return recent_traces(request.app.state.store,p,before=before,limit=limit)

    @app.get("/trace/tool/{run_id}")
    def trace(run_id:str,request:Request,turn_key:str|None=None,after:int=Query(default=0,ge=0),limit:int=Query(default=200,ge=1,le=500),through:int|None=Query(default=None,ge=0),p=Depends(principal)):
        from resolvedesk.trace_queries import trace_page
        return trace_page(request.app.state.store,p,run_id,turn_key,after=after,limit=limit,through=through)

    @app.post("/eval/run",status_code=202)
    async def evaluation(request:Request,data:EvaluationOptions|None=None,p=Depends(principal)):
        options = data or EvaluationOptions()
        return request.app.state.evaluations.submit(p,**options.model_dump())

    @app.post('/eval/datasets',status_code=201)
    def save_evaluation_dataset(data:EvalDataset,request:Request,p=Depends(principal)):
        return request.app.state.evaluations.datasets.create(p,data.model_dump())

    @app.get('/eval/datasets')
    def evaluation_datasets(request:Request,p=Depends(principal)):
        return request.app.state.evaluations.datasets.list(p)

    @app.get('/eval/datasets/{suite_id}')
    def evaluation_dataset(suite_id:str,request:Request,p=Depends(principal)):
        return request.app.state.evaluations.datasets.get(p,suite_id)

    @app.get('/eval/retention')
    def evaluation_retention(request:Request,days:int=30,job_id:str|None=None,p=Depends(principal)):
        return request.app.state.evaluations.retention.preview(p,days=days,job_id=job_id)

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
    async def resume_evaluation(job_id:str,request:Request,data:ResumeEvaluation|None=None,p=Depends(principal)):
        return request.app.state.evaluations.resume(p,job_id,(data or ResumeEvaluation()).extra_seconds)

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
