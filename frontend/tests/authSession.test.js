import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readSession,saveSession,clearSession } from '../src/lib/authSession.js'
import { visibleNavigation } from '../src/lib/identity.js'
const storage=()=>{const data=new Map();return {getItem:k=>data.get(k),setItem:(k,v)=>data.set(k,v),removeItem:k=>data.delete(k)}}
test('a session is bound to its service and expires without trusting cached roles',()=>{
 const s=storage();saveSession('/api',{token:'synthetic',expires_at:100,principal:{role:'support'}},s)
 assert.equal(readSession('/other',s,1),null)
 assert.deepEqual(readSession('/api',s,1),{token:'synthetic',expires_at:100})
 assert.equal(readSession('/api',s,101),null)
})
test('logout removes only this service session',()=>{
 const s=storage();saveSession('/a',{token:'a',expires_at:100},s);saveSession('/b',{token:'b',expires_at:100},s)
 clearSession('/a',s);assert.equal(readSession('/a',s,1),null);assert.equal(readSession('/b',s,1).token,'b')
})
test('employee and unknown identities have no support or evaluation navigation',()=>{
 const views=[{id:'chat'},{id:'admin'},{id:'evaluation'}]
 for(const role of ['','user'])assert.deepEqual(visibleNavigation(views,role),[{id:'chat'}])
 assert.equal(visibleNavigation(views,'support').length,3)
})
