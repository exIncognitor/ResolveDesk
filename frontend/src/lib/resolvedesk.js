import { pollEvaluation } from './evaluationJobs.js'
export { createInitialSettings, saveSettings, backendMeta } from './backends.js'
import { backendMeta } from './backends.js'

export async function deskRequest(settings,path,method='GET',body,signal) {
  const response=await fetch(backendMeta('python',settings).baseUrl+path,{method,
    headers:{Authorization:`Bearer ${settings.pythonToken}`,'Content-Type':'application/json'},
    body:body===undefined?undefined:JSON.stringify(body),signal:signal||AbortSignal.timeout(120000)})
  const data=await response.json()
  if(!response.ok){const error=new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail));error.status=response.status;throw error}
  return data
}
export const requestHealth=(_,s)=>deskRequest(s,'/health')
export const requestMonitor=(_,s)=>deskRequest(s,'/monitor')
export const requestSkills=(_,s)=>deskRequest(s,'/skills')
export const reloadSkills=(_,s)=>deskRequest(s,'/skills/reload','POST')
export const requestKnowledgeStats=(_,s)=>deskRequest(s,'/knowledge/stats')
export const requestSearch=async(_,s,query,top_k=5)=>({results:(await deskRequest(s,'/knowledge/search','POST',{query,top_k})).items})
export async function requestToolTrace(_,s,id,turnKey){
  let result,after=0,through,turn=turnKey
  for(let page=0;page<100;page++){
    const q=new URLSearchParams();if(turn)q.set('turn_key',turn);if(after)q.set('after',after);if(through!=null)q.set('through',through)
    const r=await deskRequest(s,`/trace/tool/${id}${q.size?'?'+q:''}`)
    if(!result)result=r
    else {result.trace.events.push(...r.trace.events);result.trace.tool_calls.push(...r.trace.tool_calls);result.trace.fallback_events.push(...(r.trace.fallback_events||[]))}
    turn??=r.trace.turn_key;through??=r.trace.snapshot_end
    const next=r.trace.next_after
    if(next==null){result.trace.tools_used=[...new Set((result.trace.tool_calls||[]).map(c=>c.tool_name))];return {...result,trace:{...result.trace,toolsUsed:result.trace.tools_used,toolCalls:result.trace.tool_calls}}}
    if(next<=after)throw Error('轨迹分页游标未前进')
    after=next
  }
  throw Error('轨迹超过单次读取上限，请按页查询')
}
export async function addKnowledge(_,s,documents){return deskRequest(s,'/knowledge/add','POST',{documents})}
export async function uploadKnowledge(_,s,file){
  if(file.size>10*1024*1024)throw Error('单次上传不超过 10 MiB')
  const body=new FormData();body.append('file',file,file.name)
  const response=await fetch(backendMeta('python',s).baseUrl+'/knowledge/upload',{method:'POST',headers:{Authorization:`Bearer ${s.pythonToken}`},body,signal:AbortSignal.timeout(180000)})
  const data=await response.json()
  if(!response.ok){const error=Error(typeof data.detail==='string'?data.detail:'文件导入失败');error.status=response.status;throw error}
  return data
}
export const runEvaluation=async(_,s,body,options={})=>{const job=await deskRequest(s,'/eval/run','POST',body||{kind:'retrieval'});const report=await pollEvaluation(job,id=>deskRequest(s,`/eval/jobs/${id}`),options);return report?{...report,job_id:job.job_id}:null}

// An incremental UTF-8 parser: chunks may split lines, JSON or Chinese characters.
export async function consumeEvents(response,onEvent,isCurrent=()=>true){
  if(!response.ok)throw Error(`进度连接失败：${response.status}`)
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer=''
  try{
    while(isCurrent()){
      const {done,value}=await reader.read();buffer+=done?decoder.decode():decoder.decode(value,{stream:true})
      let boundary
      while((boundary=buffer.indexOf('\n\n'))>=0){
        const block=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2)
        let kind='message',id='',data=[]
        for(const line of block.split('\n')){if(line.startsWith('event: '))kind=line.slice(7);if(line.startsWith('id: '))id=line.slice(4);if(line.startsWith('data: '))data.push(line.slice(6))}
        if(data.length&&isCurrent())onEvent({kind,id,payload:JSON.parse(data.join('\n'))})
      }
      if(done)break
    }
  }finally{await reader.cancel();reader.releaseLock()}
}

const attempts=new Map()
export async function requestChat(_,s,message,{onEvent=()=>{},isCurrent=()=>true,signal}={}){
  let run
  const key=JSON.stringify([s.pythonToken,s.conversationId,message])
  let attempt=attempts.get(key)
  if(!attempt){
    let path='/runs'
    if(s.conversationId){
      try{const current=await deskRequest(s,`/runs/${s.conversationId}`);if(['awaiting_input','awaiting_confirmation','completed'].includes(current.status))path=`/runs/${current.id}/reply`}catch(e){if(e.status!==404)throw e}
    }
    attempt={path,request_key:crypto.randomUUID()};attempts.set(key,attempt)
  }
  run=await deskRequest(s,attempt.path,'POST',{message,request_key:attempt.request_key},signal)
  // The UI adopts a newly created conversation before SSE finishes. A retry
  // after losing that stream must still reuse the original creation receipt.
  attempt.keys??=new Set([key])
  const adoptedKey=JSON.stringify([s.pythonToken,run.id,message])
  attempt.keys.add(adoptedKey);attempts.set(adoptedKey,attempt)
  onEvent({kind:'run',payload:run})
  const turnKey=run.turn_key||attempt.request_key
  const events=await fetch(backendMeta('python',s).baseUrl+`/runs/${run.id}/events?after=${run.event_cursor||0}&turn_key=${encodeURIComponent(turnKey)}`,{headers:{Authorization:`Bearer ${s.pythonToken}`},signal:signal||AbortSignal.timeout(120000)})
  onEvent({kind:'turn_start',payload:{}})
  await consumeEvents(events,onEvent,isCurrent)
  if(!isCurrent())return null
  run=await deskRequest(s,`/runs/${run.id}/turns/${encodeURIComponent(turnKey)}`)
  for(const alias of attempt.keys)if(attempts.get(alias)===attempt)attempts.delete(alias)
  let trace={trace:{events:[]}},traceAvailable=true
  try{trace=await requestToolTrace(_,s,run.id,turnKey)}catch{traceAvailable=false}
  const turnEvents=trace.trace.events.filter(e=>e.seq>=(run.event_cursor||0)&&e.seq<=(run.event_end||Infinity))
  const route=turnEvents.filter(e=>e.kind==='routing').at(-1)?.payload||{}
  const outcome=turnEvents.filter(e=>e.kind==='execution_outcome').at(-1)?.payload||{}
  const actual=outcome.actual_roles||(route.primary?[route.primary]:[])
  return {conversationId:run.id,requestId:run.id,response:run.answer,intent:route.intent||'other',fineIntent:route.fine_intent,
    primaryAgent:actual[0]||null,agentType:actual[0]||null,requestedAgent:route.primary||null,supportingAgents:actual.slice(1),traceAvailable,toolTrace:traceAvailable?trace:null,turnKey,
    executionStatus:outcome.status||(run.error==='specialist_partial'?'partial':run.status),escalated:!!outcome.escalated,latencyMs:run.latency_ms??trace.trace.latency_ms??null,
    routingReason:route.reason||'',routingConfidence:route.confidence??null,knowledgeUsed:traceAvailable?turnEvents.some(e=>e.kind==='retrieval'):null,raw:run}
}
