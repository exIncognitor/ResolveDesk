import {test} from 'node:test'
import assert from 'node:assert/strict'
import {consumeEvents,requestChat,deskRequest,runEvaluation} from '../src/lib/resolvedesk.js'

test('SSE parser handles split UTF-8 characters, JSON and event boundaries',async()=>{
 const bytes=new TextEncoder().encode('id: 1\nevent: answer_delta\ndata: {"text":"中文"}\n\nid: 2\nevent: completed\ndata: {"answer":"完成"}\n\n')
 const response=new Response(new ReadableStream({start(c){for(const b of bytes)c.enqueue(new Uint8Array([b]));c.close()}}))
 const events=[];await consumeEvents(response,e=>events.push(e))
 assert.deepEqual(events.map(e=>e.kind),['answer_delta','completed']);assert.equal(events[0].payload.text,'中文')
})

test('stale SSE consumer does not apply events',async()=>{
 let called=false
 await consumeEvents(new Response('event: completed\ndata: {}\n\n'),()=>called=true,()=>false)
 assert.equal(called,false)
})

test('chat uses authorized run reply, streams progress and returns the final draft',async t=>{
 const calls=[],events=[],s={pythonToken:'test-token',conversationId:'run1',endpoints:{python:''},backend:'python'}
 const run={id:'run1',status:'awaiting_confirmation',answer:'确认草稿',draft:{summary:'VPN'}}
 const original=globalThis.fetch
 t.after(()=>globalThis.fetch=original)
 globalThis.fetch=async(url,options)=>{
  calls.push([url,options]);assert.equal(new Headers(options.headers).get('Authorization'),'Bearer test-token')
  if(url.endsWith('/reply'))return Response.json({...run,status:'queued'})
  if(url.includes('/events?'))return new Response('id: 1\nevent: answer_delta\ndata: {"text":"草稿"}\n\nid: 2\nevent: awaiting_confirmation\ndata: {}\n\n')
  if(url.includes('/trace/tool/'))return Response.json({trace:{events:[{seq:1,kind:'routing',payload:{intent:'technical',primary:'technical',supporting:[]}}]}})
  return Response.json(run)
 }
 const result=await requestChat('python',s,'补充信息',{onEvent:e=>events.push(e)})
 assert.equal(result.raw.status,'awaiting_confirmation');assert.equal(result.primaryAgent,'technical')
 assert.ok(calls.some(([u])=>u.endsWith('/reply')));assert.ok(!calls.some(([u])=>u==='/chat'))
 assert.equal(events.find(e=>e.kind==='answer_delta').payload.text,'草稿')
 const body=JSON.parse(calls.find(([u])=>u.endsWith('/reply'))[1].body)
 assert.ok(body.request_key);assert.ok(calls.some(([u])=>u.includes('/turns/'+body.request_key)))
})

test('ambiguous reply failure retries the same turn key and endpoint and returns that saved answer',async t=>{
 const original=globalThis.fetch;t.after(()=>globalThis.fetch=original)
 const s={pythonToken:'retry-token',conversationId:'retry-run',endpoints:{python:''}}
 const posts=[];let loseResponse=true;let key
 globalThis.fetch=async(url,options)=>{
  if(options.method==='POST'){
   const body=JSON.parse(options.body);key=body.request_key;posts.push([url,key])
   if(loseResponse){loseResponse=false;throw Error('connection dropped after commit')}
   return Response.json({id:'retry-run',turn_key:key,status:'completed',event_cursor:5,event_end:8,replayed:true})
  }
  if(url.includes('/events?'))return new Response('id: 8\nevent: completed\ndata: {}\n\n')
  if(url.includes('/turns/'))return Response.json({id:'retry-run',turn_key:key,status:'completed',answer:'original reply answer',event_cursor:5,event_end:8})
  if(url.includes('/trace/tool/'))return Response.json({trace:{events:[{seq:6,kind:'routing',payload:{primary:'technical'}},{seq:12,kind:'routing',payload:{primary:'billing'}}]}})
  return Response.json({id:'retry-run',status:'completed',answer:'later answer'})
 }
 await assert.rejects(requestChat('python',s,'retry-message'),/connection dropped/)
 const result=await requestChat('python',s,'retry-message')
 assert.deepEqual(posts[0],posts[1]);assert.ok(posts[0][0].endsWith('/reply'))
 assert.equal(result.response,'original reply answer');assert.equal(result.primaryAgent,'technical')
})

test('API permission failures remain failures',async t=>{
 const original=globalThis.fetch;t.after(()=>globalThis.fetch=original)
 globalThis.fetch=async()=>Response.json({detail:'Support role required'},{status:403})
 await assert.rejects(deskRequest({pythonToken:'user',endpoints:{python:''}},'/skills/reload','POST'),e=>e.status===403)
})

test('losing the initial SSE stream after the UI adopts the conversation does not create a paid reply on retry',async t=>{
 const original=globalThis.fetch;t.after(()=>globalThis.fetch=original)
 const s={pythonToken:'initial-retry',conversationId:'',endpoints:{python:''}}
 const posts=[];let fail=true,key
 globalThis.fetch=async(url,options)=>{
  if(options.method==='POST'){
   key=JSON.parse(options.body).request_key;posts.push([url,key])
   return Response.json({id:'adopted-run',status:'queued',turn_key:key,event_cursor:1})
  }
  if(url.includes('/events?')){
   if(fail){fail=false;throw Error('stream disconnected')}
   return new Response('id: 3\nevent: completed\ndata: {}\n\n')
  }
  if(url.includes('/turns/'))return Response.json({id:'adopted-run',answer:'saved original answer',status:'completed',event_cursor:1,event_end:3})
  if(url.includes('/trace/tool/'))return Response.json({trace:{events:[]}})
  throw Error('Retry must not choose a new reply endpoint')
 }
 const onEvent=e=>{if(e.kind==='run')s.conversationId=e.payload.id}
 await assert.rejects(requestChat('python',s,'initial message',{onEvent}),/stream disconnected/)
 const result=await requestChat('python',s,'initial message',{onEvent})
 assert.deepEqual(posts[0],posts[1]);assert.equal(posts[1][0],'/runs')
 assert.equal(result.response,'saved original answer')
})

test('quality submission preserves options and returns job identity for baseline review',async t=>{
 const original=globalThis.fetch;t.after(()=>globalThis.fetch=original)
 globalThis.fetch=async(url,options)=>{
  assert.equal(url,'/eval/run');assert.deepEqual(JSON.parse(options.body),{kind:'quality',limit:2})
  return Response.json({job_id:'quality-job',status:'completed',result:{kind:'active_service_quality',passed:2}})
 }
 const result=await runEvaluation('python',{backend:'python',pythonToken:'operator',endpoints:{python:''}},{kind:'quality',limit:2})
 assert.equal(result.job_id,'quality-job');assert.equal(result.passed,2)
})

test('inspector uses actual fallback role and retains planned role and partial status',async t=>{
 const original=globalThis.fetch;t.after(()=>globalThis.fetch=original)
 const s={pythonToken:'fallback-token',conversationId:'fallback-run',endpoints:{python:''}}
 globalThis.fetch=async(url,options)=>{
  const final={id:'fallback-run',status:'completed',answer:'fallback answer',event_cursor:1,event_end:5}
  if(options.method==='POST')return Response.json({...final,status:'queued'})
  if(url.includes('/events?'))return new Response('id: 5\nevent: completed\ndata: {}\n\n')
  if(url.includes('/trace/tool/')){
   assert.ok(url.includes('turn_key='))
   return Response.json({trace:{events:[{seq:2,kind:'routing',payload:{primary:'technical',confidence:.79}},{seq:4,kind:'execution_outcome',payload:{status:'partial',actual_roles:['general'],escalated:false}}]}})
  }
  return Response.json(final)
 }
 const result=await requestChat('python',s,'fallback message')
 assert.equal(result.primaryAgent,'general');assert.equal(result.requestedAgent,'technical')
 assert.equal(result.executionStatus,'partial');assert.equal(result.routingConfidence,.79)
 assert.equal(result.latencyMs,null)
})

test('a trace outage does not discard an already completed paid answer or invent role metadata',async t=>{
 const original=globalThis.fetch;t.after(()=>globalThis.fetch=original)
 const s={pythonToken:'trace-outage-token',conversationId:'answered-run',endpoints:{python:''}}
 let posts=0
 globalThis.fetch=async(url,options)=>{
  const run={id:'answered-run',status:'completed',answer:'durably saved answer',event_cursor:1,event_end:4}
  if(options.method==='POST'){posts++;return Response.json({...run,status:'queued'})}
  if(url.includes('/events?'))return new Response('id: 4\nevent: completed\ndata: {}\n\n')
  if(url.includes('/trace/tool/'))return Response.json({detail:'temporary trace outage'},{status:503})
  return Response.json(run)
 }
 const result=await requestChat('python',s,'network question')
 assert.equal(result.response,'durably saved answer');assert.equal(posts,1)
 assert.equal(result.traceAvailable,false);assert.equal(result.primaryAgent,null)
 assert.equal(result.knowledgeUsed,null);assert.equal(result.routingConfidence,null)
})
