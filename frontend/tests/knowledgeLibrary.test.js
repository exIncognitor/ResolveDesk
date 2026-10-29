import { test } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { createRequestState } from '../src/lib/requestState.js'
import { createInitialSettings } from '../src/lib/backends.js'
const source=fs.readFileSync(new URL('../src/components/KnowledgeLibrary.vue',import.meta.url),'utf8')
test('an old manually saved service address cannot redirect the login endpoint',()=>{
  const previous=globalThis.localStorage
  globalThis.localStorage={getItem:()=>JSON.stringify({endpoints:{python:'https://old-service.example'},conversationId:'old-run'})}
  try{assert.equal(createInitialSettings().endpoints.python,'')}
  finally{globalThis.localStorage=previous}
})
function context(request){
  const settings={backend:'python',pythonToken:'employee',endpoints:{python:''}}
  const c=vm.createContext({requests:createRequestState(settings),deskRequest:request,catalog:{value:{items:[],total:0}},selected:{value:null},preview:{value:null},loading:{value:false},previewLoading:{value:false},error:{value:''}})
  for(const name of ['load','open']){
    const start=source.indexOf(`async function ${name}(`)
    const rest=source.slice(start+1),next=rest.search(/\n(?:async )?function /)
    vm.runInContext(source.slice(start,start+1+next),c)
  }
  return {c,settings}
}
test('catalog loads visible documents without submitting a search or model call',async()=>{
  const paths=[],{c}=context(async(_,path)=>{paths.push(path);return {items:[{title:'VPN'}],total:1,limit:6,offset:0}})
  await c.load();assert.deepEqual(paths,['/knowledge/documents?limit=6&offset=0']);assert.equal(c.catalog.value.total,1)
})
test('old preview response cannot appear after a role switch',async()=>{
  let resolve;const {c,settings}=context(()=>new Promise(done=>resolve=done))
  const task=c.open({document_id:'private',title:'old'});c.requests.invalidate();settings.pythonToken='new';c.preview.value=null
  resolve({items:[{content:'old private text'}]});await task;assert.equal(c.preview.value,null)
})
test('latest selected document wins when preview responses arrive out of order',async()=>{
  const pending=[],{c}=context(()=>new Promise(done=>pending.push(done)))
  const a=c.open({document_id:'a'}),b=c.open({document_id:'b'})
  pending[1]({document_id:'b'});await b;pending[0]({document_id:'a'});await a
  assert.equal(c.preview.value.document_id,'b')
})
test('catalog errors have a distinct failure message instead of an empty-library state',async()=>{
  const {c}=context(async()=>{throw Error('unavailable')});await c.load();assert.match(c.error.value,/文档目录加载失败/);assert.equal(c.loading.value,false)
})
