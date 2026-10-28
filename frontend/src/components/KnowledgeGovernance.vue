<template>
  <section class="workspace-card governance">
    <div class="card-heading"><div><span class="kicker">PUBLICATION</span><h2>知识审核与发布</h2></div><button @click="load" :disabled="busy">刷新</button></div>
    <p>客服角色可审核。技术参考不能替代企业制度；草稿、撤回和过期材料不会用于普通检索。修改内容或元数据后须重新发布。</p>
    <p role="status">{{ status }}</p>
    <p v-if="!documents.length && !status">暂无可管理的知识。新增材料会进入草稿，补齐来源和适用范围后才能审核发布。</p>
    <article v-for="document in documents" :key="document.document_id">
      <h3>{{ document.title }} · {{ publicationLabel(document) }}</h3>
      <p>{{ document.document_id }} · 内容 {{ document.version }} · 元数据 {{ document.revision }}</p>
      <p>{{ document.metadata.material_type === 'technical_reference' ? '技术参考' : document.metadata.material_type === 'simulated_policy' ? '模拟制度' : '待补材料类型' }} · {{ document.metadata.applicability.join('、') || '待补适用范围' }} · 来源核对 {{ document.metadata.source_checked_on || '待补日期' }}</p>
      <p v-if="document.metadata_complete">生效 {{ date(document.effective_from) }} · 到期 {{ date(document.expires_at) }}</p>
      <p v-else>请先补齐材料类型、来源、适用范围和有效期。存量兼容材料保存后也将进入草稿。</p>
      <p v-for="url in document.metadata.source_urls" :key="url"><a :href="url" target="_blank" rel="noopener noreferrer">{{ url }}</a></p>
      <p>审核人 {{ document.reviewer || '待审核' }} · {{ document.note }}</p>
      <label>审核或变更说明<input v-model="notes[document.document_id]" maxlength="2000" placeholder="填写来源核对和适用范围，或撤回原因" /></label>
      <button :disabled="busy || !document.metadata_complete || !notes[document.document_id]?.trim()" @click="review(document, 'publish')">审核发布</button>
      <button :disabled="busy || document.state === 'legacy' || !notes[document.document_id]?.trim()" @click="review(document, 'withdraw')">撤回</button>
      <details><summary>补齐或编辑发布信息</summary>
        <div v-if="metadata[document.document_id]" class="metadata-form">
          <label>材料类型<select v-model="metadata[document.document_id].material_type"><option disabled value="">请选择</option><option value="technical_reference">官方技术参考</option><option value="simulated_policy">模拟企业制度</option></select></label>
          <label>官方来源链接（每行一个）<textarea v-model="metadata[document.document_id].source_urls" rows="2" placeholder="https://learn.microsoft.com/..." /></label>
          <label>适用系统与版本（每行一个）<textarea v-model="metadata[document.document_id].applicability" rows="2" placeholder="例如：Windows 11、Microsoft Entra ID" /></label>
          <fieldset><legend>业务范围</legend><label v-for="domain in domains" :key="domain.value"><input v-model="metadata[document.document_id].domains" type="checkbox" :value="domain.value" />{{ domain.label }}</label></fieldset>
          <label>来源核对日期<input v-model="metadata[document.document_id].source_checked_on" type="date" /></label>
          <label>生效时间（本地时间）<input v-model="metadata[document.document_id].effective_from" type="datetime-local" /></label>
          <label>到期时间（本地时间）<input v-model="metadata[document.document_id].expires_at" type="datetime-local" /></label>
          <button :disabled="busy || !notes[document.document_id]?.trim()" @click="edit(document)">保存为草稿</button>
        </div>
      </details>
      <details><summary @click="audit(document)">审计记录</summary><p v-for="entry in audits[document.document_id] || []" :key="entry.id">{{ date(entry.created) }} · {{ entry.actor }} · {{ entry.action }} · r{{ entry.revision }} · {{ entry.note }}</p></details>
    </article>
  </section>
</template>

<script setup>
import { onMounted, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { deskRequest } from '../lib/resolvedesk'
import { createRequestState } from '../lib/requestState'
import { publicationLabel, reviewBody, publicationDraft, metadataBody } from '../lib/knowledgePublication'
const props = defineProps({ settings: { type: Object, required: true } })
const requests = createRequestState(props.settings)
const documents = ref([]), busy = ref(false), status = ref('')
const notes = reactive({}), metadata = reactive({}), audits = reactive({})
const domains = [{ value: 'account_access', label: '账号权限' }, { value: 'network_endpoint', label: '网络终端' }, { value: 'software_license', label: '软件授权' }, { value: 'device_service', label: '设备服务' }]
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
    for (const document of data) metadata[document.document_id] = publicationDraft(document)
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
  try { return mutate(document, 'metadata', 'PUT', metadataBody(document, metadata[document.document_id], notes[document.document_id])) }
  catch (error) { status.value = error.message }
}
async function audit(document) {
  const r = requests.begin(`audit:${document.document_id}`)
  try { const data = await deskRequest(r.settings, `/knowledge/publications/${encodeURIComponent(document.document_id)}/audit`); if (r.isCurrent()) audits[document.document_id] = data }
  catch (error) { if (r.isCurrent()) status.value = error.message }
}
onMounted(load)
defineExpose({ load })
</script>

<style scoped>
.governance { margin-top: 24px; }
article { border-top: 1px solid var(--border, #ddd); padding: 20px 0; }
a { overflow-wrap: anywhere; }
button { margin: 8px 10px 8px 0; }
textarea { width: 100%; font-family: monospace; }
.metadata-form { display: grid; gap: 12px; margin-top: 14px; }
.metadata-form label { display: grid; gap: 6px; }
.metadata-form fieldset label { display: inline-flex; align-items: center; margin-right: 15px; }
</style>
