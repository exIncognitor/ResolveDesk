import { test } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { verifiedRole, visibleNavigation } from '../src/lib/identity.js'
import { createRequestState } from '../src/lib/requestState.js'

const views = [{ id: 'chat' }, { id: 'knowledge' }, { id: 'operations' }, { id: 'service' }, { id: 'evaluation' }]
function settings() { return { backend: 'python', pythonToken: 'synthetic-support', userId: 'display', endpoints: { python: '/' } } }
function proof(s, role = 'support') { return { backend: s.backend, token: s.pythonToken, endpoint: s.endpoints.python, role, user: 'operator' } }

test('navigation trusts verified server identity rather than editable display data', () => {
  const s = settings()
  s.role = 'support'
  assert.equal(verifiedRole(s, null), '')
  assert.equal(visibleNavigation(views, verifiedRole(s, null)).some(v => v.id === 'evaluation'), false)
  assert.equal(verifiedRole(s, proof(s)), 'support')
  assert.equal(visibleNavigation(views, verifiedRole(s, proof(s))).some(v => v.id === 'evaluation'), true)
  assert.equal(visibleNavigation(views, 'user').some(v => v.id === 'evaluation'), false)
})

test('token and endpoint changes revoke old management navigation synchronously', () => {
  const s = settings(), identity = proof(s)
  s.pythonToken = 'synthetic-employee'
  assert.equal(verifiedRole(s, identity), '')
  assert.equal(visibleNavigation(views, verifiedRole(s, identity)).some(v => v.id === 'evaluation'), false)
  s.pythonToken = identity.token
  s.endpoints.python = '/other-service'
  assert.equal(verifiedRole(s, identity), '')
})

test('server display-name hydration preserves a verified role for the same token', () => {
  const s = settings(), identity = proof(s)
  s.userId = 'new-display-name'
  assert.equal(verifiedRole(s, identity), 'support')
})

const source = fs.readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
function healthContext(extra = {}) {
  const s = settings()
  const c = vm.createContext({ settings: s, requestState: createRequestState(s), identity: { value: null },
    healthOk: { value: false }, healthLabel: { value: '' }, statusText: { value: '' },
    requestHealth: async () => ({ status: 'ok' }), deskRequest: async () => ({ user: 'operator', role: 'support' }), ...extra })
  const start = source.indexOf('async function checkHealth(')
  const next = source.slice(start + 1).search(/\n(?:async )?function /)
  vm.runInContext(source.slice(start, start + 1 + next), c)
  return c
}

test('health binds role to the request token and endpoint after /me succeeds', async () => {
  const c = healthContext()
  await c.checkHealth()
  assert.equal(c.settings.userId, 'operator')
  assert.equal(verifiedRole(c.settings, c.identity.value), 'support')
  assert.equal(c.healthOk.value, true)
})

test('a delayed support /me response cannot grant privileges to the next employee token', async () => {
  let resolve, entered
  const waiting = new Promise(done => { entered = done })
  const c = healthContext({ deskRequest: () => { entered(); return new Promise(done => { resolve = done }) } })
  const task = c.checkHealth()
  await waiting
  c.settings.pythonToken = 'employee-next'; c.requestState.invalidate()
  resolve({ user: 'old-operator', role: 'support' })
  await task
  assert.equal(c.identity.value, null)
  assert.equal(verifiedRole(c.settings, c.identity.value), '')
})

test('rejected identity verification clears previously verified management access', async () => {
  const c = healthContext({ deskRequest: async () => { throw Error('Unauthorized') } })
  c.identity.value = proof(c.settings)
  await c.checkHealth()
  assert.equal(c.identity.value, null)
  assert.equal(c.healthOk.value, false)
})

test('stale model configuration cannot populate the next identity workspace', async () => {
  const s = settings(), requests = createRequestState(s)
  let resolve
  const c = vm.createContext({ settings: s, requestState: requests, isSupport: { value: true },
    modelInfo: { value: null }, deskRequest: () => new Promise(done => { resolve = done }) })
  const start = source.indexOf('async function loadModelConfiguration(')
  const next = source.slice(start + 1).search(/\n(?:async )?function /)
  vm.runInContext(source.slice(start, start + 1 + next), c)
  const task = c.loadModelConfiguration()
  s.pythonToken = 'employee-next'; requests.invalidate(); c.isSupport.value = false
  resolve({ model: { model: 'old-private-provider' } })
  await task
  assert.equal(c.modelInfo.value, null)
})

