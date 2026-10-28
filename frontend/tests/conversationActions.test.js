import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { createRequestState } from '../src/lib/requestState.js'
const source = fs.readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b }); return {promise,resolve,reject} }
function setup(overrides = {}) {
  const settings = { backend:'python',pythonToken:'synthetic-A',userId:'a',conversationId:'A',endpoints:{python:'/api'} }
  const context = vm.createContext({ settings,requestState:createRequestState(settings),activeRun:{value:{id:'A',approval:'approve-A'}},busy:{value:false},pendingAction:{value:''},messages:{value:[]},streamText:{value:''},lastResponse:{value:null},lastTrace:{value:null},activeView:{value:'operations'},activeStreamController:undefined,AbortController,URLSearchParams,createMessageId:()=>1,showToast:()=>assert.fail('unexpected toast'),requestToolTrace:async()=>({trace:{}}),...overrides })
  for (const name of ['confirmDraft','pauseRun','resumeRun','restoreRun','submitFeedback']) {
    const start=source.indexOf(`async function ${name}(`), next=source.slice(start+1).search(/\n(?:async )?function /)
    vm.runInContext(source.slice(start,next<0?source.indexOf('</script>',start):start+1+next),context)
  }
  return context
}
test('confirmation keeps captured run and approval, and ignores a response after selecting another run', async () => {
  const gate=deferred(),calls=[]
  const c=setup({deskRequest:async(s,path,method,body)=>{calls.push([s.pythonToken,path,body]);return gate.promise}})
  const task=c.confirmDraft();c.activeRun.value={id:'B'};c.busy.value=true
  gate.resolve({id:'RD-A'});await task
  assert.deepEqual(calls,[['synthetic-A','/runs/A/confirm',{approval:'approve-A'}]])
  assert.equal(c.messages.value.length,0);assert.equal(c.activeRun.value.id,'B');assert.equal(c.busy.value,true)
})
test('same-identity confirmation finishes once and displays its own persisted ticket', async () => {
  const calls=[],toasts=[]
  const c=setup({showToast:s=>toasts.push(s),deskRequest:async(s,path,method)=>{calls.push(path);return method==='POST'?{id:'RD-A'}:{id:'A',ticket_id:'RD-A',status:'completed'}}})
  await c.confirmDraft()
  assert.deepEqual(calls,['/runs/A/confirm','/runs/A']);assert.match(c.messages.value[0].content,/RD-A/);assert.equal(c.busy.value,false)
})
test('old history and errors cannot reappear after identity changes', async () => {
  for (const fail of [false,true]) {
    const gate=deferred(),entered=deferred()
    const c=setup({deskRequest:async(s,path)=>{if(path.endsWith('/messages')){entered.resolve();return gate.promise}return {id:'A'}}})
    const task=c.restoreRun('A');await entered.promise
    c.settings.pythonToken='synthetic-B';c.requestState.invalidate();c.activeRun.value=null;c.messages.value=[];c.busy.value=true
    if(fail)gate.reject(Error('old error'));else gate.resolve([{role:'assistant',content:'private old history'}])
    await task;assert.equal(c.messages.value.length,0);assert.equal(c.activeRun.value,null);assert.equal(c.busy.value,true)
  }
})
test('the latest selected history wins and trace failure does not hide messages', async () => {
  const old=deferred()
  const c=setup({deskRequest:async(s,path)=>path==='/runs/A'?old.promise:path.endsWith('/messages')?[{role:'assistant',content:'B history'}]:{id:'B',answer:'B answer'},requestToolTrace:async()=>{throw Error('trace unavailable')}})
  const first=c.restoreRun('A');await c.restoreRun('B');old.resolve({id:'A'});await first
  assert.equal(c.activeRun.value.id,'B');assert.equal(c.messages.value[0].content,'B history');assert.equal(c.lastTrace.value,null)
})
test('resume pins identity and turn cursor, and cannot consume an old stream after switching', async () => {
  const gate=deferred(),entered=deferred(),calls=[];let receivedSignal,cancelled=false
  const c=setup({deskRequest:async(s,path)=>({id:'A',status:'running',event_cursor:9,turn_key:'original-turn'}),backendMeta:()=>({baseUrl:'/api'}),fetch:async(url,options)=>{calls.push([url,options.headers.Authorization]);receivedSignal=options.signal;entered.resolve();return gate.promise},consumeEvents:async()=>assert.fail('stale stream consumed'),handleRunEvent:()=>assert.fail('stale event applied')})
  const task=c.resumeRun();await entered.promise
  c.activeStreamController.abort();c.requestState.invalidate();c.settings.pythonToken='synthetic-B';c.activeRun.value={id:'B'};c.busy.value=true
  gate.resolve({body:{cancel:async()=>{cancelled=true}}});await task
  assert.ok(receivedSignal.aborted);assert.equal(cancelled,true);assert.equal(c.busy.value,true)
  assert.deepEqual(calls,[['/api/runs/A/events?after=9&turn_key=original-turn','Bearer synthetic-A']])
})
test('pause fences the old stream and feedback cannot post a toast into another conversation', async () => {
  const gate=deferred();let aborted=false
  const c=setup({activeStreamController:{abort:()=>{aborted=true}},deskRequest:async()=>gate.promise})
  const task=c.pauseRun();assert.ok(aborted);c.activeRun.value={id:'B'};gate.resolve({id:'A',status:'paused'});await task;assert.equal(c.activeRun.value.id,'B')
  const feedback=deferred();c.deskRequest=async()=>feedback.promise
  const pending=c.submitFeedback('good');c.activeRun.value={id:'C'};feedback.resolve({saved:true});await pending
})
