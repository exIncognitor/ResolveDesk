"""Owned real HTTP MCP fixture with auth, catalog notification and restart."""
import asyncio
from contextlib import asynccontextmanager
import socket

import uvicorn
from mcp.server.fastmcp import FastMCP,Context
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse


class HTTPFixture:
    def __init__(self,token):
        self.token=token;self.port=0;self.server=None;self.task=None
        self.calls=0;self.refreshes=0

    async def start(self):
        fixture=self
        mcp=FastMCP('Owned HTTP MCP fixture',json_response=False,streamable_http_path='/mcp')
        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False))
        async def external_check(category:str)->dict:
            fixture.calls+=1
            # Deliberately echo synthetic credentials, including a JSON key,
            # to verify transport credentials never reach the Agent/debug API.
            return {'category':category,'revision':fixture.refreshes,'credential':fixture.token,fixture.token:'synthetic echo'}
        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False))
        async def refresh_catalog(ctx:Context)->dict:
            fixture.refreshes+=1
            await ctx.session.send_tool_list_changed()
            return {'revision':fixture.refreshes}
        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=True))
        async def dangerous_write(value:str)->dict:return {'synthetic_write':value}
        protocol=mcp.streamable_http_app()
        class Auth:
            async def __call__(self,scope,receive,send):
                if scope['type']=='http' and dict(scope.get('headers',[])).get(b'authorization')!=('Bearer '+fixture.token).encode():
                    return await JSONResponse({'detail':'Unauthorized'},status_code=401)(scope,receive,send)
                return await protocol(scope,receive,send)
        @asynccontextmanager
        async def lifespan(app):
            async with mcp.session_manager.run():yield
        from starlette.applications import Starlette
        from starlette.routing import Mount
        app=Starlette(routes=[Mount('/',app=Auth())],lifespan=lifespan)
        listener=socket.socket();listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        listener.bind(('127.0.0.1',self.port));self.port=listener.getsockname()[1]
        listener.listen(128);listener.setblocking(False)
        self.server=uvicorn.Server(uvicorn.Config(app,log_level='error',timeout_graceful_shutdown=1))
        self.task=asyncio.create_task(self.server.serve(sockets=[listener]))
        async with asyncio.timeout(8):
            while not self.server.started:
                if self.task.done():await self.task;raise RuntimeError('Fixture failed to start')
                await asyncio.sleep(.01)

    async def stop(self):
        if self.task:
            self.server.should_exit=True
            await asyncio.wait_for(self.task,8)
            self.task=None

    @property
    def url(self):return f'http://127.0.0.1:{self.port}/mcp'
