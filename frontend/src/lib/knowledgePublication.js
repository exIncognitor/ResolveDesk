export function reviewBody(document, action, note) {
  if (!['publish', 'withdraw'].includes(action) || !note?.trim()) throw new Error('请填写审核或撤回说明')
  return { action, version: document.version, revision: document.revision, note: note.trim() }
}

export function publicationLabel(document) {
  if (document.state === 'legacy') return '存量兼容 · 待纳管'
  if (document.state === 'draft') return '草稿 · 待审核'
  if (document.state === 'withdrawn') return '已撤回'
  return document.readable ? '已发布 · 可检索' : '已发布 · 未生效或已到期'
}

function localDate(value) {
  const date = new Date(value)
  const two = n => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${two(date.getMonth() + 1)}-${two(date.getDate())}T${two(date.getHours())}:${two(date.getMinutes())}`
}

export function publicationDraft(document, now = Date.now()) {
  const m = document.metadata || {}
  return {
    material_type: document.metadata_complete ? m.material_type : '',
    source_urls: (m.source_urls || []).join('\n'), applicability: (m.applicability || []).join('\n'),
    domains: [...(m.domains || [])], source_checked_on: m.source_checked_on || localDate(now).slice(0, 10),
    effective_from: localDate(m.effective_from ? m.effective_from * 1000 : now),
    expires_at: localDate(m.expires_at ? m.expires_at * 1000 : now + 90 * 86400000)
  }
}

export function metadataBody(document, draft, note) {
  if (!note?.trim()) throw new Error('请填写审核或变更说明')
  if (!['technical_reference', 'simulated_policy'].includes(draft.material_type)) throw new Error('请选择材料类型')
  const lines = value => [...new Set(value.split(/\r?\n/).map(s => s.trim()).filter(Boolean))]
  const effective = new Date(draft.effective_from).getTime() / 1000, expires = new Date(draft.expires_at).getTime() / 1000
  if (!Number.isFinite(effective) || !Number.isFinite(expires) || expires <= effective) throw new Error('请填写正确的生效和到期时间')
  const sources = lines(draft.source_urls), applicability = lines(draft.applicability)
  if (draft.material_type === 'technical_reference' && !sources.length) throw new Error('技术参考需要填写官方来源链接')
  if (!applicability.length || !draft.domains.length) throw new Error('请填写适用系统和业务范围')
  return { version: document.version, revision: document.revision, note: note.trim(), metadata: {
    material_type: draft.material_type, source_urls: sources, applicability, domains: [...draft.domains],
    source_checked_on: draft.source_checked_on, effective_from: effective, expires_at: expires
  } }
}

export function citationText(source) {
  const lines = [`${source.title} · ${source.section} · ${source.version}`, source.content]
  const p = source.publication
  if (p) lines.push(`材料：${p.metadata.material_type === 'technical_reference' ? '技术参考（不代表企业制度）' : '模拟制度'}`,
    `适用：${p.metadata.applicability.join('、')}`, `发布元数据版本：${p.revision}`,
    `来源核对：${p.metadata.source_checked_on}`, `有效期：${new Date(p.metadata.effective_from * 1000).toLocaleString()} 至 ${new Date(p.metadata.expires_at * 1000).toLocaleString()}`,
    ...p.metadata.source_urls.map(url => `来源：${url}`))
  return lines.join('\n')
}
