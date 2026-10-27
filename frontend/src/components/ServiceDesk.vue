<template>
  <section class="page page-service">
    <div class="page-heading"><div><span class="kicker">IT SERVICE DESK</span><h1>业务服务台</h1><p>模拟企业数据 · 不会修改真实账号、设备或厂商授权</p></div><button @click="load" :disabled="busy">刷新</button></div>
    <p role="status">{{ status }}</p>
    <section class="workspace-card"><h2>资源与本人资产</h2>
      <p>权限资源：{{ Object.keys(catalog.resources || {}).join('、') }}；最长 30 天，审批后另行开通。</p>
      <p v-for="asset in catalog.my_assets || []" :key="asset.id">{{ asset.id }} · {{ asset.model }} · {{ asset.warranty === 'in_warranty' ? '保修期内' : '已过保' }} · {{ asset.status === 'in_repair' ? '维修中' : '可用' }}</p>
      <p v-for="grant in catalog.my_grants || []" :key="grant.resource">有效权限：{{ grant.resource }} / {{ grant.level }}，到期 {{ new Date(grant.expires * 1000).toLocaleString() }}</p>
      <p v-for="license in catalog.licenses || []" :key="license.product">{{ license.product }} · {{ license.approval }} · 演示席位 {{ license.available_seats ?? '不适用' }}</p>
      <button @click="$emit('compose', '请帮我准备权限申请，先核对可申请资源并询问缺少的信息。')">申请权限</button>
      <button @click="$emit('compose', '请帮我准备本人设备维修申请，先核对本人资产并询问缺少的信息。')">设备报修</button>
    </section>
    <section class="workspace-card"><h2>申请与维修记录</h2><p v-if="!cases.length">暂无业务流程。通过对话准备草稿并确认后，会显示在这里。</p>
      <article v-for="item in cases" :key="item.ticket_id" class="service-case">
        <h3>{{ item.fields.summary }} · {{ item.ticket_id }}</h3><p>{{ item.kind === 'access_request' ? '权限申请' : '设备维修' }} · {{ states[item.status] || item.status }} · 申请人 {{ item.owner }}</p>
        <p>{{ item.fields.description }}</p><p v-if="item.assignee">处理人：{{ item.assignee }}</p>
        <label v-if="item.allowed_actions.length">处理或验收说明<input v-model="notes[item.ticket_id]" maxlength="2000" placeholder="填写审批依据、排障结果或验收情况" /></label>
        <button v-for="action in item.allowed_actions" :key="action" :disabled="busy || !notes[item.ticket_id]?.trim()" @click="act(item, action)">{{ labels[action] }}</button>
        <details><summary>审计记录</summary><p v-for="(entry, index) in item.audit" :key="index">{{ entry.actor }} · {{ entry.action }} · {{ entry.to_status }} · {{ entry.note }}</p></details>
      </article>
    </section>
    <section v-if="monitor.window_seconds" class="workspace-card"><h2>监控与告警</h2><p>统计窗口 {{ monitor.window_seconds }} 秒，至少 {{ monitor.min_samples }} 个样本；累计主执行 {{ monitor.total_requests }} 次、会话 {{ monitor.total_conversations ?? '—' }} 个。角色和工具分别统计。</p>
      <p v-for="alert in monitor.active_alerts" :key="alert.id">{{ alert.payload.name }}：{{ alert.payload.message }}</p>
      <p v-if="!monitor.active_alerts?.length">当前没有活跃告警。</p>
      <article v-for="(suggestion,index) in monitor.suggestions || []" :key="index"><strong>{{ suggestion.title }}</strong><p>{{ suggestion.action }}</p></article>
      <p>{{ monitor.webhook_configured ? '已配置 Webhook；下方显示投递结果。' : '未配置外部 Webhook，告警事件保存在本地。' }}</p>
      <details><summary>工具、角色指标与通知记录</summary><pre>{{ JSON.stringify(monitor, null, 2) }}</pre></details>
    </section>
  </section>
</template>

<script setup>
import { onMounted, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { deskRequest } from '../lib/resolvedesk.js'
import { createRequestState } from '../lib/requestState.js'
const props = defineProps({ settings: { type: Object, required: true } })
defineEmits(['compose'])
const requests = createRequestState(props.settings)
const catalog = ref({}), cases = ref([]), monitor = ref({}), status = ref(''), busy = ref(false)
const notes = reactive({}), attempts = new Map()
const labels = { approve: '批准申请', reject: '驳回申请', provision: '模拟开通', cancel: '取消申请', triage: '完成分诊', assign: '分派给我', start: '开始处理', resolve: '提交处理结果', accept: '验收并关闭', reopen: '验收不通过，返工' }
const states = { pending_approval: '待审批', approved: '已批准，待开通', fulfilled: '已模拟开通', rejected: '已驳回', cancelled: '已取消', submitted: '已提交，待分诊', triaged: '已分诊，待派单', assigned: '已派单', in_progress: '处理中', resolved: '处理完成，待验收', closed: '已验收关闭' }
async function load() {
  const request = requests.begin('load')
  try {
    const [nextCatalog, nextCases, nextMonitor] = await Promise.all([deskRequest(request.settings, '/service-catalog'), deskRequest(request.settings, '/service-cases'), deskRequest(request.settings, '/monitor')])
    if (!request.isCurrent()) return
    catalog.value = nextCatalog; cases.value = nextCases; monitor.value = nextMonitor
  } catch (error) { if (request.isCurrent()) status.value = error.message }
}
async function act(item, action) {
  if (busy.value) return
  const request = requests.begin('action'); busy.value = true
  const payload = { action, version: item.version, note: notes[item.ticket_id].trim(), assignee: action === 'assign' ? request.settings.userId : '' }
  // /me supplies the authoritative operator identity for assignment.
  try {
    if (action === 'assign') payload.assignee = (await deskRequest(request.settings, '/me')).user
    if (!request.isCurrent()) return
    const key = JSON.stringify([item.ticket_id, payload])
    if (!attempts.has(key)) attempts.set(key, crypto.randomUUID())
    await deskRequest(request.settings, `/service-cases/${item.ticket_id}/actions`, 'POST', { ...payload, request_key: attempts.get(key) })
    if (!request.isCurrent()) return
    attempts.delete(key); notes[item.ticket_id] = ''; status.value = '操作已保存。'; await load()
  } catch (error) { if (request.isCurrent()) { status.value = error.message; await load() } }
  finally { if (request.isCurrent()) busy.value = false }
}
watch(() => [props.settings.pythonToken, props.settings.endpoints?.python], () => { requests.invalidate(); catalog.value = {}; cases.value = []; monitor.value = {}; status.value = ''; busy.value = false; attempts.clear(); for (const key of Object.keys(notes)) delete notes[key]; load() })
onMounted(load)
onBeforeUnmount(() => requests.invalidate())
</script>
