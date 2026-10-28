import test from 'node:test'
import assert from 'node:assert/strict'
import { reviewBody, publicationLabel, citationText, publicationDraft, metadataBody } from '../src/lib/knowledgePublication.js'
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

test('legacy enrollment keeps revision zero and converts form values without mutating the source', () => {
  const document = { state: 'legacy', version: 'original', revision: 0, metadata: {}, metadata_complete: false }
  const draft = publicationDraft(document, Date.UTC(2026, 9, 28))
  assert.equal(draft.material_type, '')
  Object.assign(draft, { material_type: 'technical_reference', source_urls: ' https://learn.microsoft.com/ \nhttps://learn.microsoft.com/', applicability: ' Windows 11 \nWindows 11', domains: ['network'] })
  const body = metadataBody(document, draft, ' 核对来源并纳管 ')
  assert.equal(body.revision, 0); assert.equal(body.version, 'original')
  assert.deepEqual(body.metadata.source_urls, ['https://learn.microsoft.com/'])
  assert.deepEqual(body.metadata.applicability, ['Windows 11'])
  assert.ok(body.metadata.expires_at > body.metadata.effective_from)
  assert.deepEqual(document.metadata, {})
  assert.match(publicationLabel(document), /待纳管/)
})

test('metadata form rejects incomplete publication evidence and reversed dates', () => {
  const draft = { ...publicationDraft({}, Date.UTC(2026, 9, 28)), material_type: 'technical_reference', applicability: 'Windows', domains: ['network'] }
  assert.throws(() => metadataBody({}, draft, 'review'), /官方来源/)
  draft.source_urls = 'https://learn.microsoft.com/'
  assert.throws(() => metadataBody({}, { ...draft, domains: [] }, 'review'), /业务范围/)
  assert.throws(() => metadataBody({}, { ...draft, expires_at: draft.effective_from }, 'review'), /时间/)
  assert.throws(() => metadataBody({}, draft, ''), /说明/)
})
