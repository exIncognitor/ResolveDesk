import test from 'node:test'
import assert from 'node:assert/strict'
import { requestChat } from '../src/lib/resolvedesk.js'

test('a submitted conversation queries the same run and never opens another request', async t => {
  const original = globalThis.fetch; t.after(() => globalThis.fetch = original)
  const calls = [], run = { id: 'submitted', status: 'completed', ticket_id: 'RD-TEST', answer: 'fulfilled' }
  globalThis.fetch = async (url, options) => {
    calls.push([url, options.method])
    if (url.includes('/events?')) return new Response('event: completed\ndata: {}\n\n')
    if (url.includes('/trace/tool/')) return Response.json({ trace: { events: [] } })
    return Response.json(run)
  }
  const result = await requestChat('python', { pythonToken: 'test-only', conversationId: run.id, endpoints: { python: '' } }, '查询工单进度')
  assert.equal(result.raw.ticket_id, run.ticket_id)
  assert.ok(calls.some(([url, method]) => url === '/runs/submitted/reply' && method === 'POST'))
  assert.ok(!calls.some(([url, method]) => url === '/runs' && method === 'POST'))
})
