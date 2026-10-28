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

export function citationText(source) {
  const lines = [`${source.title} · ${source.section} · ${source.version}`, source.content]
  const p = source.publication
  if (p) lines.push(`材料：${p.metadata.material_type === 'technical_reference' ? '技术参考（不代表企业制度）' : '模拟制度'}`,
    `适用：${p.metadata.applicability.join('、')}`, `发布元数据版本：${p.revision}`,
    `来源核对：${p.metadata.source_checked_on}`, `有效期：${new Date(p.metadata.effective_from * 1000).toLocaleString()} 至 ${new Date(p.metadata.expires_at * 1000).toLocaleString()}`,
    ...p.metadata.source_urls.map(url => `来源：${url}`))
  return lines.join('\n')
}
