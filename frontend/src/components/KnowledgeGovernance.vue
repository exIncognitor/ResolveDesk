<template>
  <section class="workspace-card governance">
    <div class="card-heading"><div><span class="kicker">PUBLICATION</span><h2>知识审核与发布</h2></div><button @click="load" :disabled="busy">刷新</button></div>
    <p>客服角色可审核。技术参考不能替代企业制度；草稿、撤回和过期材料不会用于普通检索。修改内容或元数据后须重新发布。</p>
    <p role="status">{{ status }}</p>
    <p v-if="!documents.length">暂无可管理的知识。可上传带 metadata 的 JSON 文档进入草稿；已有无元数据文档沿用原来的发布方式。</p>
    <article v-for="document in documents" :key="document.document_id">
      <h3>{{ document.title }} · {{ publicationLabel(document) }}</h3>
      <p>{{ document.document_id }} · 内容 {{ document.version }} · 元数据 {{ document.revision }}</p>
      <p>{{ document.metadata.material_type === 'technical_reference' ? '技术参考' : '模拟制度' }} · {{ document.metadata.applicability.join('、') }} · 来源核对 {{ document.metadata.source_checked_on }}</p>
      <p>生效 {{ date(document.effective_from) }} · 到期 {{ date(document.expires_at) }}</p>
      <p v-for="url in document.metadata.source_urls" :key="url"><a :href="url" target="_blank" rel="noopener noreferrer">{{ url }}</a></p>
      <p>审核人 {{ document.reviewer || '待审核' }} · {{ document.note }}</p>
      <label>审核或变更说明<input v-model="notes[document.document_id]" maxlength="2000" placeholder="填写来源核对和适用范围，或撤回原因" /></label>
      <button :disabled="busy || !notes[document.document_id]?.trim()" @click="review(document, 'publish')">审核发布</button>
      <button :disabled="busy || !notes[document.document_id]?.trim()" @click="review(document, 'withdraw')">撤回</button>
      <details><summary>编辑来源与适用元数据</summary><label>元数据 JSON<textarea v-model="metadata[document.document_id]" rows="9" /></label><button :disabled="busy || !notes[document.document_id]?.trim()" @click="edit(document)">保存为草稿</button></details>
      <details><summary @click="audit(document)">审计记录</summary><p v-for="entry in audits[document.document_id] || []" :key="entry.id">{{ date(entry.created) }} · {{ entry.actor }} · {{ entry.action }} · r{{ entry.revision }} · {{ entry.note }}</p></details>
    </article>
  </section>
</template>

<script setup>
import { onMounted, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { deskRequest } from '../lib/resolvedesk'
import { createRequestState } from '../lib/requestState'
import { publicationLabel, reviewBody } from '../lib/knowledgePublication'
const props = defineProps({ settings: { type: Object, required: true } })
const requests = createRequestState(props.settings)
const documents = ref([]), busy = ref(false), status = ref('')
const notes = reactive({}), metadata = reactive({}), audits = reactive({})
const date = value => new Date(value * 1000).toLocaleString()
function reset() { requests.invalidate(); documents.value = []; busy.value = false; status.value = ''; for (const map of [notes, metadata, audits]) for (const key of Object.keys(map)) delete map[key] }
watch(() => [props.settings.backend, props.settings.pythonToken, props.settings.endpoints?.python], () => { reset(); load() })
onBeforeUnmount(reset)
async function load() {
  const r = requests.begin('publication'); busy.value = true
  try {
    const data = await deskRequest(r.settings, '/knowledge/publications')
    if (!r.isCurrent()) return
    documents.value = data
    for (const document of data) metadata[document.document_id] = JSON.stringify(document.metadata, null, 2)
    status.value = `${data.length} 篇受管理材料`
  } catch (error) { if (r.isCurrent()) { documents.value = []; status.value = error.message } }
  finally { if (r.isCurrent()) busy.value = false }
}
async function mutate(document, suffix, method, body) {
  const r = requests.begin('publication'); busy.value = true
  try {
    await deskRequest(r.settings, `/knowledge/publications/${encodeURIComponent(document.document_id)}/${suffix}`, method, body)
    if (!r.isCurrent()) return
    await load()
  } catch (error) { if (r.isCurrent()) status.value = error.message }
  finally { if (r.isCurrent()) busy.value = false }
}
function review(document, action) {
  try { return mutate(document, 'review', 'POST', reviewBody(document, action, notes[document.document_id])) }
  catch (error) { status.value = error.message }
}
function edit(document) {
  try { return mutate(document, 'metadata', 'PUT', { version: document.version, revision: document.revision, note: notes[document.document_id], metadata: JSON.parse(metadata[document.document_id]) }) }
  catch { status.value = '元数据 JSON 格式不正确' }
}
async function audit(document) {
  const r = requests.begin(`audit:${document.document_id}`)
  try { const data = await deskRequest(r.settings, `/knowledge/publications/${encodeURIComponent(document.document_id)}/audit`); if (r.isCurrent()) audits[document.document_id] = data }
  catch (error) { if (r.isCurrent()) status.value = error.message }
}
onMounted(load)
</script>

<style scoped>
.governance { margin-top: 24px; }
article { border-top: 1px solid var(--border, #ddd); padding: 20px 0; }
a { overflow-wrap: anywhere; }
button { margin: 8px 10px 8px 0; }
textarea { width: 100%; font-family: monospace; }
</style>
