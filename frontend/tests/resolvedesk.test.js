import {test} from 'node:test'
import assert from 'node:assert/strict'
import {consumeEvents,requestChat,deskRequest} from '../src/lib/resolvedesk.js'

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
  if(url.includes('/trace/tool/'))return Response.json({trace:{events:[{kind:'routing',payload:{intent:'technical',primary:'technical',supporting:[]}}]}})
  return Response.json(run)
 }
 const result=await requestChat('python',s,'补充信息',{onEvent:e=>events.push(e)})
 assert.equal(result.raw.status,'awaiting_confirmation');assert.equal(result.primaryAgent,'technical')
 assert.ok(calls.some(([u])=>u.endsWith('/reply')));assert.ok(!calls.some(([u])=>u==='/chat'))
 assert.equal(events.find(e=>e.kind==='answer_delta').payload.text,'草稿')
})

test('API permission failures remain failures',async t=>{
 const original=globalThis.fetch;t.after(()=>globalThis.fetch=original)
 globalThis.fetch=async()=>Response.json({detail:'Support role required'},{status:403})
 await assert.rejects(deskRequest({pythonToken:'user',endpoints:{python:''}},'/skills/reload','POST'),e=>e.status===403)
})
