"""Per-application standard MCP knowledge server with per-request API identity.

Stateless HTTP prevents connection/session identity reuse between workers or
users. Every request, tool and resource is scoped by the same account table as
the normal API; tenant/user are never accepted as tool arguments.
"""
import asyncio
from contextvars import ContextVar
import hashlib
import json
import os
import re
from typing import Annotated,Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel,Field
from starlette.responses import JSONResponse

from resolvedesk.store import Principal


class KnowledgeResult(BaseModel):
    results:list[dict]
    source:str='resolvedesk_knowledge'
    mode:str
    effective_mode:str
    degraded:bool
    errors:list[str]


def create_knowledge_mcp(api):
    actor=ContextVar("resolvedesk_knowledge_mcp_principal",default=None)
    hosts=os.getenv("RESOLVEDESK_MCP_ALLOWED_HOSTS","localhost,localhost:*,127.0.0.1,127.0.0.1:*,app,app:*,resolvedesk,resolvedesk:*,resolvedesk-python,resolvedesk-python:*")
    origins=os.getenv("RESOLVEDESK_MCP_ALLOWED_ORIGINS","http://localhost,http://localhost:*,http://127.0.0.1,http://127.0.0.1:*")
    server=FastMCP("ResolveDesk IT Knowledge",stateless_http=True,json_response=True,streamable_http_path="/",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=[h.strip() for h in hosts.split(',') if h.strip()],allowed_origins=[h.strip() for h in origins.split(',') if h.strip()]))

    def principal():
        p=actor.get()
        if p is None:raise ToolError("Authenticated request identity required")
        return p

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True))
    async def search_local_knowledge(query:Annotated[str,Field(min_length=1,max_length=2000)],
                                     top_k:Annotated[int,Field(ge=1,le=20,strict=True)]=5,
                                     mode:Literal['hybrid','bm25','vector']='hybrid')->KnowledgeResult:
        """按当前令牌身份检索IT知识库。只读，不调用模型，不接受租户或用户覆盖。"""
        p=principal()
        try:
            services=api.state.agent.services
            data=await services.search(p,query,top_k,optimize=False,mode=mode) if services else await asyncio.to_thread(api.state.knowledge.search,p,query,top_k,mode)
            return KnowledgeResult(results=data['items'],mode=data['mode'],effective_mode=data.get('effective_mode',data['mode']),
                degraded=data['degraded'],errors=data.get('errors',[]))
        except asyncio.CancelledError:raise
        except Exception:raise ToolError("Knowledge search unavailable or invalid request") from None

    @server.resource("resolvedesk://knowledge/stats")
    async def knowledge_stats()->str:
        """当前令牌可见的知识片段计数，不含其他用户私有片段。"""
        p=principal()
        def read():
            with api.state.store.db() as db:
                return db.execute("SELECT COUNT(*) FROM chunks WHERE tenant=? AND (visibility='tenant' OR owner=?)",(p.tenant,p.user)).fetchone()[0]
        return json.dumps({'total_chunks':await asyncio.to_thread(read)})

    @server.resource("resolvedesk://knowledge/citations/{citation}")
    async def knowledge_citation(citation:str)->str:
        """读取当前权限和版本有效的引用原文；旧版本或无权限返回错误。"""
        p=principal()
        if not re.fullmatch(r"C-[a-f0-9]{24}",citation):raise ValueError("Invalid citation")
        try:return json.dumps(await asyncio.to_thread(api.state.knowledge.citation,p,citation),ensure_ascii=False)
        except Exception:raise ValueError("Citation unavailable") from None

    protocol=server.streamable_http_app()
    class AuthenticatedProtocol:
        async def __call__(self,scope,receive,send):
            if scope['type']!='http':return await protocol(scope,receive,send)
            headers=dict(scope.get('headers',[]));authorization=headers.get(b'authorization',b'').decode('latin-1')
            token=authorization[7:] if authorization.startswith('Bearer ') else ''
            account=getattr(api.state,'accounts',{}).get(hashlib.sha256(token.encode()).hexdigest()) if token else None
            if not account or account.get('role')not in {'user','support'}:
                return await JSONResponse({'detail':'Bearer token required'},status_code=401,headers={'WWW-Authenticate':'Bearer'})(scope,receive,send)
            marker=actor.set(Principal(account['tenant'],account['user'],account['role']))
            try:return await protocol(scope,receive,send)
            finally:actor.reset(marker)
    return server,AuthenticatedProtocol()
