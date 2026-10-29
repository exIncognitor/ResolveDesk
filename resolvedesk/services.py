"""Live integration of retrieval optimization, roles, Skills, memory and MCP."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import time
import uuid

from agents.agent_orchestrator import AgentOrchestrator, AgentType, Request
from core.skill_loader import SkillManager
from resolvedesk.store import encode
from resolvedesk.memory import ScopedMemory
from tooling.mcp_client import StandardMCPClient
from tooling.tool_manager import MCPToolManager

ROOT = Path(__file__).resolve().parents[1]


class DeskServices:
    def __init__(self,store,knowledge,client,mcp_configs=None,*,coordination=None):
        import os
        self.store,self.knowledge,self.client = store,knowledge,client
        self.execution_policy=os.getenv('RESOLVEDESK_EXECUTION_POLICY','full')
        if self.execution_policy not in {'full','adaptive'}:raise ValueError('Unknown execution policy')
        from resolvedesk.monitoring import Monitor
        self.monitor = Monitor(store)
        self.skills = SkillManager(ROOT/"skills",max_prompt_chars=3500)
        self.skills.load()
        self.memory = ScopedMemory(store,knowledge.embedding,client,coordination=coordination)
        self.memory_vectors = None
        from resolvedesk.entity_state import EntityState
        from resolvedesk.intent import ITIntentRecognizer
        from resolvedesk.roles import IT_AGENTS
        self.entities=EntityState(store)
        self.remote_embedding=None
        provider=os.getenv("RESOLVEDESK_INTENT_VECTOR_PROVIDER","local_e5")
        if provider not in {"local_e5","self_hosted_http"}:raise ValueError("Unknown IT intent vector provider")
        if provider=="self_hosted_http":
            from resolvedesk.remote_embedding import HTTPEmbedding
            self.remote_embedding=HTTPEmbedding(os.getenv("RESOLVEDESK_INTENT_VECTOR_URL",""),os.getenv("RESOLVEDESK_EMBEDDING_TOKEN",""))
        recognizer=ITIntentRecognizer(client,self.remote_embedding or knowledge.embedding)
        model=getattr(client,'model','deepseek-flash')
        self.roles = AgentOrchestrator(api_key='',model=model,skill_manager=self.skills,agent_classes=IT_AGENTS,intent_recognizer=recognizer)
        # Runtime nodes share the configured client; historical budgets are not used.
        self.roles._composer._client = client
        for pool in self.roles._pool.values():
            for agent in pool:
                agent._client = client
                agent._model = model
                agent.profile = replace(agent.profile,model=model)
        self.manager = MCPToolManager(api_key='',model=model)
        self.manager._client = client
        self.manager.observer=self._observe_tool
        from resolvedesk.reliable_retrieval import ReliableRetrieval
        self.retrieval=ReliableRetrieval(knowledge,client,self.manager,coordination)
        if mcp_configs is None:
            from tooling.mcp_client import load_servers
            configured=os.getenv("RESOLVEDESK_MCP_SERVERS_CONFIG")
            if not configured and os.getenv("MCP_EXTERNAL_ENABLED","").lower()=="true":configured=os.getenv("MCP_SERVERS_CONFIG")
            path=Path(configured) if configured else ROOT/"deploy/mcp_servers.json"
            if not path.is_absolute():path=ROOT/path
            configs=load_servers(path)
        else:configs=mcp_configs
        # Writing tools may not bypass the confirmed-ticket transaction.
        if any(not c.read_only for c in configs):
            raise ValueError("Only read-only MCP servers are enabled")
        self.mcp = StandardMCPClient(self.manager,configs,on_change=self._wire_role_tools)
        self._wire_role_tools()
        self.maintenance_task = None
        self.routing_health={}
        self.probe_cooldown=30.
        self.observer_slots=asyncio.Semaphore(8)

    async def start(self):
        await self.mcp.start()
        if self.knowledge.vector_index:
            from resolvedesk.vectors import ChromaVectors
            # A separate collection prevents private conversations from entering
            # tenant-visible knowledge search, including in isolated fixtures.
            name="resolvedesk-memory-"+hashlib.sha256(self.knowledge.vector_index.name.encode()).hexdigest()[:16]
            self.memory_vectors=await asyncio.to_thread(ChromaVectors,str(self.knowledge.vector_index.http.base_url),self.knowledge.embedding.identity,collection=name)
            self.memory.vector_index=self.memory_vectors
        await self.memory.start()
        if self.knowledge.vector_index:
            async def maintenance():
                while True:
                    try:
                        await asyncio.to_thread(self.knowledge.drain_index)
                        await asyncio.to_thread(self.store.recover_expired)
                        from resolvedesk.licenses import expire_due
                        await asyncio.to_thread(expire_due,self.store)
                    except asyncio.CancelledError: raise
                    except Exception: pass  # Durable outbox retains failure evidence.
                    await asyncio.sleep(2)
            self.maintenance_task = asyncio.create_task(maintenance())

    async def close(self):
        await self.memory.close()
        if self.maintenance_task:
            self.maintenance_task.cancel()
            await asyncio.gather(self.maintenance_task,return_exceptions=True)
        await self.mcp.close()
        await self.manager._client.close()
        if self.remote_embedding:self.remote_embedding.close()
        if self.memory_vectors:self.memory_vectors.close()

    def _wire_role_tools(self):
        from resolvedesk.roles import build_it_tools
        tools=build_it_tools(self)
        for name,spec in self.mcp.agent_tools().items():
            async def call(req,args,name=name):
                if req.principal is None or req.executing_role is None:raise PermissionError("Server scope required")
                return await self.call_external(req.principal,name,args,req.executing_role)
            tools[name]=replace(spec,handler=call)
        self.roles.set_shared_tools(tools)

    async def route(self,p,run_id,message,history,context):
        previous=self.entities.get(p,run_id)
        result=await self.roles._intent_recognizer.recognize(message,history[-4:],context={"domain":previous["domain"],"entities":previous["entities"]})
        state=self.entities.merge(p,run_id,message,result.domain)
        mapping = {"account_access": AgentType.GENERAL, "network_endpoint": AgentType.TECHNICAL,
                   "software_license": AgentType.BILLING, "device_service": AgentType.TECHNICAL,
                   "escalation": AgentType.ESCALATION, "general": AgentType.GENERAL}
        roles=list(dict.fromkeys(mapping[d] for d in result.domains))
        measured=self._monitor_snapshot(p.tenant)
        penalties=measured['routing_penalties']
        scores={r.value:(1/(1+i*.15))*(1-penalties.get(r.value,0)) for i,r in enumerate(roles)}
        primary=max(roles,key=lambda r:scores[r.value])
        degraded = [r.value for r in roles if penalties.get(r.value)]
        health=self.routing_health.setdefault((p.tenant,primary.value),{"retry_at":0.,"probe":False,"recovered":False})
        now=time.monotonic()
        if health["probe"] and now>=health.get("probe_until",now+1):health["probe"]=False
        probe=False
        if result.urgency.name=="CRITICAL":primary=AgentType.ESCALATION
        elif penalties.get(primary.value,0)>=self.monitor.policy['fallback_penalty']:
            if not health["retry_at"] and not health["recovered"]:health["retry_at"]=now+self.probe_cooldown
            if health["recovered"]:pass
            elif now>=health["retry_at"] and not health["probe"]:
                health.update(probe=True,probe_until=now+120);probe=True
            else:primary=AgentType.GENERAL if primary!=AgentType.GENERAL else AgentType.ESCALATION
        else:health.update(retry_at=0.,probe=False,recovered=False)
        supporting = [r for r in roles if r != primary and penalties.get(r.value,0)<self.monitor.policy['fallback_penalty']]
        if result.needs_clarification:primary,supporting=AgentType.GENERAL,[]
        profile=self.roles._pool[primary][0].profile
        return {"intent":result.domain,"fine_intent":result.fine_intent,"domains":result.domains,"intent_method":result.method,
            "primary":primary.value,"supporting":[r.value for r in supporting],"confidence":result.confidence,
            "source_scores":result.source_scores,"embedding_source":result.embedding_source,"urgency":result.urgency.name,
            "needs_clarification":result.needs_clarification,"recovery_probe":probe,"entities":state["entities"],"entity_state":state,
            'routing_scores':scores,'monitor_penalties':penalties,
            'monitor_available':measured.get('monitor_available',True),
            "reason":result.reasoning+("；异常节点降权："+",".join(degraded) if degraded else ""),"mission":profile.mission,"workflow":list(profile.workflow)}

    async def consult(self,p,run_id,message,history,context,routing,cached=None,on_result=None):
        from core.intent_recognizer import UrgencyLevel
        from resolvedesk.intent import DOMAIN_CATEGORY
        from resolvedesk.tracing import redact
        cached=cached or {}
        sources={item["citation"]:item for item in context.get("evidence",[])}
        import re
        direct=(self.execution_policy=='adaptive' and len(message)<=160 and routing.get('confidence',0)>=.75
            and routing.get('fine_intent') in {'vpn_failure','dns_failure','software_install','software_activation','account_unlock','license_request'}
            and len(routing.get('domains',[]))==1 and not routing.get('supporting') and routing.get('urgency')=='LOW'
            and not routing.get('needs_clarification') and routing.get('monitor_available',True) and not routing.get('monitor_penalties')
            and bool(sources) and not context.get('retrieval_degraded') and not cached
            and not re.search(r'申请|准备|提交|报修|维修|开通|借用|更换|鼓包|冒烟|泄露|更正|不是|换个|新问题',message))
        self.store.event(p,run_id,'execution_plan',{'policy':self.execution_policy,'path':'direct_master' if direct else 'specialist_collaboration',
            'reason':'single_low_risk_domain_with_current_evidence' if direct else 'full_context_required'})
        if direct:
            return {'status':'success','specialist_opinions':[],'sources':sources,'actual_roles':[],'escalated':False,'execution_path':'direct_master'}
        turn=self.store.get_run(p,run_id)
        instance_penalties=self._monitor_snapshot(p.tenant)['instance_penalties']
        observed_durations={}
        async def record(event):
            observed_durations.setdefault(event['request_id'],{})[event['role']]=event['seconds']*1000
            try:self.store.event(p,run_id,'agent_execution',{k:v for k,v in event.items() if k!='tools'})
            except Exception:pass
            self.monitor.record(p.tenant,'agent',event['role'],event['success'],event['seconds'],owner=p.user,instance=event['instance'],
                status=event['status'],event_key='agent:'+event['request_id']+':'+event['instance'],run_id=run_id,turn_key=turn.get('turn_key'),detail={'error_code':event['error_code']})
            for index,tool in enumerate(event.get('tools',[])):
                self.monitor.record(p.tenant,'tool',tool['tool_name'],tool.get('success',False) and tool.get('result_success')is not False,tool.get('latency_ms',0)/1000,
                    owner=p.user,instance=event['instance'],event_key=f'tool:{event["request_id"]}:{event["instance"]}:{index}',run_id=run_id,turn_key=turn.get('turn_key'),detail={'layer':'specialist','cached':bool(tool.get('cached'))})
        async def execute(role):
            if role in cached and cached[role]["status"]!="failed":return cached[role]
            async def trace_tool(payload):
                await self._persist_trace(p,run_id,turn.get('turn_key'),payload)
            req=Request(message=message,user_id=hashlib.sha256(encode([p.tenant,p.user]).encode()).hexdigest(),conv_id=run_id,
                context=encode(context),history=history,entities=routing.get("entities",{}),intent=DOMAIN_CATEGORY[routing["intent"]],
                intent_group=routing["intent"],urgency=UrgencyLevel[routing.get("urgency","LOW")],intent_confidence=routing.get("confidence",0),
                request_id=uuid.uuid4().hex,principal=p,authorized_sources=sources,on_agent_execution=record,on_tool_trace=trace_tool,instance_penalties=instance_penalties)
            started=time.monotonic()
            try:
                response=await self.roles._execute(req,AgentType(role))
                fallback=any(t.get("event")=="agent_fallback" for t in response.tool_traces)
                total_ms=round((time.monotonic()-started)*1000,3)
                node_ms=observed_durations.get(req.request_id,{}).get(response.agent_type.value)
                if node_ms is None:node_ms=response.latency_ms if response.latency_ms>0 else total_ms
                result={"requested_role":role,"role":response.agent_type.value,"status":response.status if response.success else "failed",
                    "success":response.success and response.status=="success","answer":response.content[:1800],"fallback":fallback,
                    "escalate":response.escalate,"tools_used":response.tools_used,"tool_traces":redact(response.tool_traces),
                    "latency_ms":node_ms,'total_latency_ms':total_ms,"error_code":response.error_code}
            except asyncio.CancelledError:
                health=self.routing_health.get((p.tenant,role))
                if health and health["probe"]:health.update(probe=False,retry_at=time.monotonic()+self.probe_cooldown)
                raise
            except Exception as exc:
                result={"requested_role":role,"role":role,"status":"failed","success":False,"answer":"专业节点暂不可用。",
                    "fallback":False,"escalate":False,"tools_used":[],"tool_traces":[],"latency_ms":round((time.monotonic()-started)*1000,3),"error_code":type(exc).__name__}
            self.store.event(p,run_id,"specialist_result",redact({k:v for k,v in result.items() if k!="answer"}))
            health=self.routing_health.get((p.tenant,role))
            if health:
                healthy=result["success"] and result["role"]==role and not result["fallback"] and result['latency_ms']<=self.monitor.policy['agent_avg_ms']
                health.update(probe=False,recovered=healthy,retry_at=0. if healthy else time.monotonic()+self.probe_cooldown)
            if on_result:await on_result(role,result,dict(sources))
            return result
        roles=[routing["primary"]]+routing.get("supporting",[])
        opinions=await asyncio.gather(*(execute(role) for role in roles))
        status="failed" if all(r["status"]=="failed" for r in opinions) else "partial" if any(r["status"]!="success" or r["fallback"] for r in opinions) else "success"
        return {"status":status,"specialist_opinions":opinions,"sources":sources,
            "actual_roles":list(dict.fromkeys(r["role"] for r in opinions)),"escalated":routing.get("urgency")=="CRITICAL" or routing["intent"]=="escalation" or any(r["escalate"] for r in opinions)}

    async def search(self,p,query,top_k=5,optimize=True,mode="hybrid",role=None):
        result=await self.retrieval.search(p,query,top_k,optimize,mode,role)
        self.monitor.safe_record(p.tenant,"retrieval",mode,bool(result["items"]) and not result["errors"] and not result["degraded"],result["latency_ms"]/1000,owner=p.user,instance=self.monitor.worker_id,detail={'cached':result['cached'],'degraded':result['degraded']})
        return result

    def publish_tool_health(self,tenant):
        health={}
        for name,tool in list(self.manager._tools.items()):
            breaker=tool.breaker
            health[name]={'circuit_state':breaker.state.value,'retry_after_s':max(0,breaker.recovery_s-(time.monotonic()-breaker.opened_at)) if breaker.opened_at else 0,
                'failure_threshold':breaker.threshold,'timeout_s':tool.timeout_s,'cache_ttl':tool.cache_ttl,'scope':'shared_worker_dependency_state'}
        self.monitor.publish_tools(tenant,self.monitor.worker_id,health)

    def _monitor_snapshot(self,tenant):
        try:return self.monitor.refresh(tenant)
        except Exception:return {'routing_penalties':{},'instance_penalties':{},'monitor_available':False}

    async def _observe_tool(self,name,result,seconds,context,error):
        if not context.get('tenant') or not context.get('user_id'):return
        degraded=bool(result and not result.success and isinstance(result.data,dict) and result.data.get('items'))
        status='cancelled' if error=='CancelledError' else 'degraded' if degraded else 'success' if result and result.success else 'failure'
        def observe():
            self.monitor.record(context['tenant'],'tool',name,status=='success',seconds,owner=context['user_id'],instance=self.monitor.worker_id,
                status=status,detail={'layer':'manager','cached':bool(result and result.cached),'fallback':degraded,'error_code':error})
            self.publish_tool_health(context['tenant'])
        await self.observer_slots.acquire()
        task=asyncio.create_task(asyncio.to_thread(observe))
        def done(future):
            self.observer_slots.release()
            if not future.cancelled():future.exception()
        task.add_done_callback(done)
        await asyncio.shield(task)

    def external_tools(self,role):
        return {name:spec for name,spec in self.mcp.agent_tools().items() if role in (spec.allowed_agents or ()) and spec.retry_safe}

    async def _persist_trace(self,p,run_id,turn_key,payload):
        async def persist():
            await self.observer_slots.acquire()
            task=asyncio.create_task(asyncio.to_thread(self.store.trace_tool,p,run_id,turn_key,payload))
            def done(future):
                self.observer_slots.release()
                if not future.cancelled():future.exception()
            task.add_done_callback(done)
            await asyncio.shield(task)
        try:await asyncio.wait_for(persist(),.2)
        except Exception:pass

    async def call_external(self,p,name,args,role):
        if name not in self.external_tools(role):raise PermissionError("Tool is not available for this role")
        result = await self.manager.call(name,args,context={"tenant":p.tenant,"user_id":p.user,"role":role},use_cache=True)
        if not result.success:
            failure=RuntimeError('MCP tool failed')
            origin=self.mcp._origins.get(name,('',name))
            failure.trace_data={'success':False,'error_code':result.error,'cached':result.cached,'source':'external_mcp','mcp_server':origin[0],'mcp_tool':origin[1]}
            raise failure
        return {**result.data,"cached":result.cached,"source":"external_mcp",
            "mcp_server":result.data.get('server'),"mcp_tool":result.data.get('tool')}
