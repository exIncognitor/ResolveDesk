import test from 'node:test'
import assert from 'node:assert/strict'
import { reviewBody, publicationLabel, citationText } from '../src/lib/knowledgePublication.js'
import { createRequestState } from '../src/lib/requestState.js'

test('review binds both content and metadata revisions and requires note', () => {
  assert.deepEqual(reviewBody({ version: 'abc', revision: 4 }, 'withdraw', '  过期  '), { action: 'withdraw', version: 'abc', revision: 4, note: '过期' })
  assert.throws(() => reviewBody({}, 'publish', ' '))
  assert.throws(() => reviewBody({}, 'delete', 'why'))
  assert.equal(publicationLabel({ state: 'published', readable: false }), '已发布 · 未生效或已到期')
})

test('citation displays official reference scope without claiming enterprise policy', () => {
  const text = citationText({ title: 'DNS', section: '检查', version: '1', content: '正文', publication: { revision: 2, metadata: { material_type: 'technical_reference', applicability: ['Windows 11'], source_checked_on: '2026-10-27', effective_from: 1, expires_at: 2, source_urls: ['https://learn.microsoft.com/'] } } })
  assert.match(text, /不代表企业制度/)
  assert.match(text, /Windows 11/)
  assert.match(text, /https:\/\/learn.microsoft.com/)
})

test('publication requests fence replaced and identity-switched responses', () => {
  const settings = { backend: 'python', endpoints: { python: 'http://localhost' } }
  const requests = createRequestState(settings)
  const first = requests.begin('publication'), second = requests.begin('publication')
  assert.equal(first.isCurrent(), false); assert.equal(second.isCurrent(), true)
  requests.invalidate(); assert.equal(second.isCurrent(), false)
})
