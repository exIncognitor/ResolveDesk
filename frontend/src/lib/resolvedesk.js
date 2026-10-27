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
export const requestToolTrace=async(_,s,id)=>{const r=await deskRequest(s,`/trace/tool/${id}`);return {...r,trace:{...r.trace,toolsUsed:r.trace.tools_used,toolCalls:r.trace.tool_calls}}}
export async function addKnowledge(_,s,documents){const results=[];for(const d of documents)results.push(await deskRequest(s,'/knowledge/documents','POST',{...d,document_id:d.document_id||crypto.randomUUID()}));return {results}}
export async function uploadKnowledge(_,s,file){
  if(file.size>300000)throw Error('单次上传不超过 300 KB')
  const text=await file.text()
  const docs=file.name.endsWith('.json')?JSON.parse(text):[{title:file.name,content:text}]
  if(!Array.isArray(docs)||docs.length>10)throw Error('JSON 应为不超过 10 篇文档的数组')
  return addKnowledge(_,s,docs)
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
  if(s.conversationId){
    try{const current=await deskRequest(s,`/runs/${s.conversationId}`);if(['awaiting_input','awaiting_confirmation','completed'].includes(current.status)&&!current.ticket_id)run=await deskRequest(s,`/runs/${current.id}/reply`,'POST',{message})}catch(e){if(e.status!==404)throw e}
  }
  if(!run){
    const key=JSON.stringify([s.pythonToken,s.conversationId,message]);if(!attempts.has(key))attempts.set(key,crypto.randomUUID())
    run=await deskRequest(s,'/runs','POST',{message,request_key:attempts.get(key)});attempts.delete(key)
  }
  onEvent({kind:'run',payload:run})
  const events=await fetch(backendMeta('python',s).baseUrl+`/runs/${run.id}/events?after=${run.event_cursor||0}`,{headers:{Authorization:`Bearer ${s.pythonToken}`},signal:signal||AbortSignal.timeout(120000)})
  onEvent({kind:'turn_start',payload:{}})
  await consumeEvents(events,onEvent,isCurrent)
  if(!isCurrent())return null
  run=await deskRequest(s,`/runs/${run.id}`)
  const trace=await deskRequest(s,`/trace/tool/${run.id}`)
  const route=trace.trace.events.filter(e=>e.kind==='routing').at(-1)?.payload||{}
  return {conversationId:run.id,requestId:run.id,response:run.answer,intent:route.intent||'other',primaryAgent:route.primary||'general',agentType:route.primary||'general',
    supportingAgents:route.supporting||[],routingReason:route.reason||'',routingConfidence:route.confidence||0,knowledgeUsed:trace.trace.events.some(e=>e.kind==='retrieval'),raw:run}
}
