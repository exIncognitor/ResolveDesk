<template>
  <section class="page page-service">
    <div class="page-heading service-heading"><div><span class="kicker">SERVICE OPERATIONS</span><h1>业务服务台</h1><p>{{ role === 'support' ? '处理租户内的服务申请，执行审批、派单与资源操作。' : '查看本人资源、发起申请，跟进每一项服务的办理进度。' }}</p></div><div class="service-heading-actions"><span class="demo-label">{{ role === 'support' ? '服务人员视图' : '员工视图' }} · 模拟环境</span><button class="quiet-button" @click="load" :disabled="busy"><AppIcon name="refresh" />刷新</button></div></div>
    <p v-if="status" class="inline-notice" role="status">{{ status }}</p>
    <div class="service-overview" aria-label="当前身份服务概览">
      <article><span>业务申请</span><strong>{{ loaded ? cases.length : '—' }}</strong><small>当前身份可见</small><AppIcon name="document" /></article>
      <article><span>可处理事项</span><strong>{{ loaded ? cases.filter(item => item.allowed_actions?.length).length : '—' }}</strong><small>等待你的下一步</small><AppIcon name="route" /></article>
      <article><span>已结束流程</span><strong>{{ loaded ? cases.filter(item => endedStates.includes(item.status)).length : '—' }}</strong><small>保留办理记录与审计</small><AppIcon name="check" /></article>
      <article><span>本人设备</span><strong>{{ loaded ? (catalog.my_assets || []).length : '—' }}</strong><small>按当前身份查看</small><AppIcon name="monitor" /></article>
    </div>
    <div class="service-launchers" aria-label="发起服务申请">
      <button v-for="service in servicePrompts" :key="service.label" @click="$emit('compose', service.text)"><span class="service-launch-icon"><AppIcon :name="service.icon" /></span><span><strong>{{ service.label }}</strong><small>{{ service.description }}</small></span><AppIcon name="northeast" /></button>
    </div>
    <div class="service-content-grid">
    <section class="workspace-card service-case-list"><div class="card-heading"><div><span class="kicker">REQUEST TRACKING</span><h2>业务申请记录</h2></div><span class="section-count">{{ loaded ? cases.length : '—' }} 项</span></div>
      <div v-if="!cases.length" class="service-empty"><AppIcon name="document" /><h3>{{ loaded ? '还没有业务申请' : '等待加载服务记录' }}</h3><p>从上方选择服务，在对话中核对并确认申请后，即可在这里跟进。</p></div>
      <article v-for="item in cases" :key="item.ticket_id" class="service-case">
        <div class="service-case-heading"><span class="case-kind">{{ kinds[item.kind] || item.kind }}</span><span class="state-badge" :class="`status-${item.status}`">{{ states[item.status] || item.status }}</span></div>
        <h3>{{ item.fields.summary }}</h3><p class="case-reference">{{ item.ticket_id }} <span>申请人 {{ item.owner }}</span></p>
        <p class="case-description">{{ item.fields.description }}</p>
        <div class="case-facts"><span v-if="item.fields.resource_id">资源 {{ item.fields.resource_id }}</span><span v-if="item.fields.asset_id">设备 {{ item.fields.asset_id }}</span><span v-if="item.fields.product">{{ item.fields.product }}</span><span v-if="item.fields.duration_days">{{ item.fields.duration_days }} 天</span><span v-if="item.assignee">处理人 {{ item.assignee }}</span></div>
        <p v-if="item.license" class="muted">席位 {{ states[item.license.state] || item.license.state }} · 到期 {{ new Date(item.license.expires * 1000).toLocaleString() }}</p>
        <label v-if="item.allowed_actions.length">处理或验收说明<input v-model="notes[item.ticket_id]" maxlength="2000" placeholder="填写审批依据、排障结果或验收情况" /></label>
        <div class="case-actions"><button v-for="action in item.allowed_actions" :key="action" :class="{ 'quiet-button': ['reject','cancel','reopen','revoke'].includes(action) }" :disabled="busy || !notes[item.ticket_id]?.trim()" @click="act(item, action)">{{ labels[action] }}</button></div>
        <details class="case-audit"><summary>审计记录 · {{ item.audit.length }} 次操作</summary><ol><li v-for="(entry, index) in item.audit" :key="index"><strong>{{ labels[entry.action] || entry.action }}</strong><span>{{ entry.actor }} · {{ states[entry.to_status] || entry.to_status }}</span><p>{{ entry.note }}</p></li></ol></details>
      </article>
    </section>
    <aside class="service-resources workspace-card"><div class="card-heading"><div><span class="kicker">MY RESOURCES</span><h2>资源与本人资产</h2></div><AppIcon name="grid" /></div>
      <h3>可申请权限</h3><div v-for="(resource, id) in catalog.resources || {}" :key="id" class="resource-row"><strong>{{ resource.name || id }}</strong><code>{{ id }}</code><span>{{ resource.levels?.join(' / ') }} · 最长 {{ resource.max_days }} 天</span></div>
      <h3>本人设备</h3><div v-for="asset in catalog.my_assets || []" :key="asset.id" class="resource-row"><strong>{{ asset.id }} · {{ asset.model }}</strong><span>{{ asset.warranty === 'in_warranty' ? '保修期内' : '已过保' }} · {{ asset.status === 'in_repair' ? '维修中' : '可用' }}</span></div><p v-if="loaded && !catalog.my_assets?.length" class="muted">当前身份暂无设备记录。</p>
      <h3>软件席位</h3><div v-for="license in catalog.licenses || []" :key="license.product" class="resource-row"><strong>{{ license.product }}</strong><span>{{ license.available_seats == null ? '按软件白名单安装' : `可用 ${license.available_seats} / 总计 ${license.total_seats}` }}</span><small v-if="license.total_seats != null">预占 {{ license.reserved_seats }} · 分配 {{ license.allocated_seats }} · 预占 {{ license.reservation_seconds / 60 }} 分钟</small></div>
      <template v-if="catalog.my_grants?.length"><h3>有效权限</h3><div v-for="grant in catalog.my_grants" :key="grant.resource" class="resource-row"><strong>{{ grant.resource }} / {{ grant.level }}</strong><span>到期 {{ new Date(grant.expires * 1000).toLocaleString() }}</span></div></template>
    </aside>
    </div>
    <section v-if="role === 'support' && monitor.window_seconds" class="workspace-card"><h2>监控与告警</h2><p>统计窗口 {{ monitor.window_seconds }} 秒，至少 {{ monitor.min_samples }} 个样本；累计主执行 {{ monitor.total_requests }} 次、会话 {{ monitor.total_conversations ?? '—' }} 个。角色和工具分别统计。</p>
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
import AppIcon from './AppIcon.vue'
import { deskRequest } from '../lib/resolvedesk.js'
import { createRequestState } from '../lib/requestState.js'
const props = defineProps({ settings: { type: Object, required: true }, role: { type: String, default: '' } })
defineEmits(['compose'])
const requests = createRequestState(props.settings)
const catalog = ref({}), cases = ref([]), monitor = ref({}), status = ref(''), busy = ref(false)
const loaded = ref(false)
const endedStates = ['fulfilled','closed','revoked','expired','rejected','cancelled']
const servicePrompts = [
  { label: '申请权限', icon: 'key', description: '按需访问，审批后开通', text: '请帮我准备权限申请，先核对可申请资源并询问缺少的信息。' },
  { label: '设备报修', icon: 'monitor', description: '提交故障，跟进处理与验收', text: '请帮我准备本人设备维修申请，先核对本人资产并询问缺少的信息。' },
  { label: '申请软件授权', icon: 'grid', description: '查询席位，申请与回收授权', text: '请帮我准备 IntelliJ IDEA Ultimate 模拟软件授权申请，先查询实时席位，询问我的用途和使用天数。' }
]
const notes = reactive({}), attempts = new Map()
const kinds = { access_request: '权限申请', device_repair: '设备维修', license_request: '软件授权' }
const labels = { approve: '批准申请', reject: '驳回申请', provision: '模拟开通', cancel: '取消申请', triage: '完成分诊', assign: '分派给我', start: '开始处理', resolve: '提交处理结果', accept: '验收并关闭', reopen: '验收不通过，返工', reserve: '预占模拟席位', allocate: '分配模拟席位', revoke: '回收模拟席位' }
const states = { pending_approval: '待审批', approved: '已批准，待执行', fulfilled: '已模拟开通', rejected: '已驳回', cancelled: '已取消', submitted: '已提交，待分诊', triaged: '已分诊，待派单', assigned: '已派单', in_progress: '处理中', resolved: '处理完成，待验收', closed: '已验收关闭', reserved: '已预占，待分配', allocated: '已模拟分配', revoked: '已回收', expired: '已到期回收' }
async function load() {
  const request = requests.begin('load')
  try {
    const [nextCatalog, nextCases, nextMonitor] = await Promise.all([deskRequest(request.settings, '/service-catalog'), deskRequest(request.settings, '/service-cases'), deskRequest(request.settings, '/monitor')])
    if (!request.isCurrent()) return
    catalog.value = nextCatalog; cases.value = nextCases; monitor.value = nextMonitor; loaded.value = true
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
watch(() => [props.settings.pythonToken, props.settings.endpoints?.python], () => { requests.invalidate(); catalog.value = {}; cases.value = []; monitor.value = {}; status.value = ''; busy.value = false; loaded.value = false; attempts.clear(); for (const key of Object.keys(notes)) delete notes[key]; load() })
onMounted(load)
onBeforeUnmount(() => requests.invalidate())
</script>
