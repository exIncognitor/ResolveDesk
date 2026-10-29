<template>
  <LoginPanel v-if="!currentRole" :settings="settings" :notice="authNotice" @authenticated="applySession" />
  <div v-else class="app-shell" :class="`view-${activeView}`">
    <aside class="navigation-rail" aria-label="主导航">
      <a class="brand" href="#" @click.prevent="activeView = 'chat'" aria-label="ResolveDesk 首页">
        <span class="brand-mark">RD</span>
        <span class="brand-name">ResolveDesk<small>IT SERVICE WORKSPACE</small></span>
      </a>
      <button class="new-chat-button" @click="activeView = 'chat'; clearConversation()">
        <AppIcon name="plus" />
        <span>新对话</span>
      </button>
      <span class="nav-caption">服务工作区</span>
      <nav class="view-nav">
        <button
          v-for="view in visibleViews"
          :key="view.id"
          :aria-label="view.label"
          :class="{ active: activeView === view.id }"
          :aria-current="activeView === view.id ? 'page' : undefined"
          @click="activeView = view.id"
        >
          <AppIcon :name="view.icon" />
          <span>{{ view.label }}</span>
          <AppIcon class="nav-chevron" name="chevron" />
        </button>
      </nav>
      <div class="rail-bottom">
        <div class="rail-context"><span class="rail-context-dot"></span><span>从问题到解决<small>知识 · 申请 · 服务进度</small></span></div>
        <a class="docs-link" aria-label="API 文档" :href="docsUrl" target="_blank" rel="noreferrer">
          <AppIcon name="document" />
          <span>API 文档</span>
          <AppIcon name="northeast" />
        </a>
        <div class="user-button" role="group" aria-label="当前登录账号">
          <span class="avatar">{{ userInitial }}</span>
          <span>
            <strong>{{ settings.userId || 'anonymous' }}</strong>
            <small>{{ isSupport ? 'IT 服务人员' : currentRole === 'user' ? '员工工作空间' : '未验证身份' }}</small>
          </span>
        </div>
      </div>
    </aside>
    <main class="workspace">
      <header class="topbar">
        <div class="page-breadcrumb">
          <span>服务工作台</span>
          <AppIcon name="chevron" />
          <strong>{{ currentView.label }}</strong>
        </div>
        <a class="mobile-brand" href="#" @click.prevent="activeView = 'chat'" aria-label="ResolveDesk 首页">
          <span class="brand-mark">RD</span>
          <span>ResolveDesk</span>
        </a>
        <div class="topbar-tools">
          <span class="connection-status" :title="statusText || healthLabel">
            <i :class="healthOk ? 'online' : 'offline'"></i>
            {{ healthOk ? '已连接' : healthLabel === '未检查' ? '连接中' : '未连接' }}
          </span>
          <span class="workspace-mode">模拟工作空间</span>
          <span class="session-role">{{ isSupport ? '支持人员' : '员工' }} · {{ settings.userId }}</span>
          <button v-if="demoLoginEnabled" class="text-button identity-switch" :disabled="switchingIdentity" @click="switchIdentity(isSupport?'user':'support')">{{ switchingIdentity?'切换中…':isSupport?'切换为员工':'切换为支持人员' }}</button>
          <button class="text-button" @click="logoutSession">退出登录</button>
        </div>
      </header>
      <div v-if="toast" class="toast" role="status">
        <AppIcon name="chat" />
        {{ toast }}
      </div>
      <section v-if="activeView === 'chat'" class="page-chat">
        <div class="chat-toolbar">
          <span class="conversation-label">
            {{ messages.length ? '当前对话' : '新对话' }}
            <span v-if="messages.length" class="conversation-count">{{ messages.length }} 条消息</span>
          </span>
          <div class="chat-toolbar-actions">
            <button class="icon-button" @click="clearConversation" aria-label="新对话" title="新对话">
              <AppIcon name="compose" />
            </button>
            <button class="trace-toggle" @click="inspectorDialog.showModal()">
              <AppIcon name="panel" />
              <span>运行详情</span>
            </button>
          </div>
        </div>
        <div class="conversation-layout" :class="{ 'has-conversation': messages.length }">
          <section class="conversation" :class="{ 'is-empty': !messages.length }" aria-label="客服对话">
            <div class="messages" ref="messageList" role="log" aria-label="对话消息" aria-live="polite" :aria-busy="busy && pendingAction === 'chat'">
              <div v-if="!messages.length" class="welcome">
                <span class="welcome-eyebrow"><span></span> YOUR IT SERVICE DESK</span>
                <h1>你的 IT 服务，从这里开始。</h1>
                <p>查制度、排故障、办申请。把需求说清楚，让每一步都有进度。</p>
                <div class="service-journey" aria-label="服务办理流程"><span><b>01</b> 描述需求</span><i></i><span><b>02</b> 核对申请</span><i></i><span><b>03</b> 跟踪办理</span></div>
              </div>
              <template v-else>
                <article v-for="item in messages" :key="item.id" :class="['message', item.role, { 'message-error': item.error }]">
                  <span v-if="item.role === 'assistant'" class="message-avatar">R</span>
                  <div class="message-content">
                    <div class="message-meta">
                      <strong>{{ item.role === 'user' ? '你' : 'ResolveDesk' }}</strong>
                      <span v-if="item.error" class="error-label">请求失败</span>
                      <small v-else-if="item.role === 'assistant'">{{ currentBackend.label }} Agent</small>
                    </div>
                    <p>{{ item.content }}</p>
                    <span v-if="item.meta && !item.error" class="agent-tag">
                      <AppIcon name="route" />
                      {{ item.meta }}
                    </span>
                    <details v-if="item.trace?.toolCalls?.length || item.trace?.toolsUsed?.length" class="message-trace">
                      <summary>
                        <AppIcon name="chevron" />
                        <span>工具调用</span>
                        <span class="count-tag">{{ item.trace.toolCalls?.length || 0 }}</span>
                      </summary>
                      <div v-for="(call, index) in item.trace.toolCalls" :key="`${item.id}-${index}`" class="trace-call-item">
                        <div class="trace-call-meta">
                          <strong>{{ call.tool_name || 'unknown_tool' }}</strong>
                          <span :class="call.success ? 'success' : 'danger'">{{ call.success ? '成功' : '失败' }}</span>
                        </div>
                        <pre>{{ formatJson(call.input || {}) }}</pre>
                      </div>
                      <p v-if="!item.trace.toolCalls?.length" class="muted">已调用：{{ item.trace.toolsUsed.join(' · ') }}</p>
                    </details>
                    <button
                      v-if="item.role === 'assistant' && !item.error"
                      class="icon-button copy-button"
                      @click="copyMessage(item.content)"
                      aria-label="复制回复"
                      title="复制回复"
                    >
                      <AppIcon name="copy" />
                    </button>
                  </div>
                </article>
                <div v-if="busy && pendingAction === 'chat'" class="thinking" role="status">
                  <span class="message-avatar">R</span>
                  <span class="thinking-dots">
                    <i></i>
                    <i></i>
                    <i></i>
                  </span>
                  <span>{{ progressText || '正在处理问题' }}</span>
                </div>
                <p v-if="streamText && busy" class="stream-preview">{{ streamText }}</p>
              </template>
            </div>
            <div class="composer-area">
              <form class="composer" @submit.prevent="performAction('chat', sendMessage)">
                <textarea
                  ref="composerInput"
                  v-model="draft"
                  rows="2"
                  aria-label="IT 服务需求"
                  placeholder="例如：申请内部文档的只读权限，使用 9 天，用于查阅接口文档。"
                  @keydown.meta.enter.prevent="performAction('chat', sendMessage)"
                  @keydown.ctrl.enter.prevent="performAction('chat', sendMessage)"
                >
                </textarea>
                <div class="composer-bottom">
                  <span class="routing-label">
                    <AppIcon name="route" />
                    按需求匹配服务
                  </span>
                  <div class="send-controls">
                    <span class="keyboard-hint">⌘ / Ctrl + Enter</span>
                    <button
                      class="send-button"
                      type="submit"
                      :disabled="busy || !draft.trim()"
                      :aria-label="busy && pendingAction === 'chat' ? '处理中' : '发送'"
                      :title="busy && pendingAction === 'chat' ? '处理中' : '发送'"
                    >
                      <AppIcon :name="busy && pendingAction === 'chat' ? 'refresh' : 'arrow'" :class="{ spinning: busy && pendingAction === 'chat' }" />
                    </button>
                  </div>
                </div>
              </form>
              <div v-if="!messages.length" class="prompt-grid">
                <button v-for="prompt in prompts" :key="prompt.label" @click="usePrompt(prompt.text)">
                  <AppIcon :name="prompt.icon" />
                  <span>
                    <strong>{{ prompt.label }}</strong>
                    <small>{{ prompt.description }}</small>
                  </span>
                  <AppIcon class="prompt-arrow" name="northeast" />
                </button>
              </div>
              <section v-if="activeRun" class="run-panel" aria-label="任务与工单">
                <div class="run-panel-heading"><strong>{{ activeRun.draft ? '申请信息' : '本次服务' }}</strong><span class="state-badge">{{ runStatusLabels[activeRun.status] || activeRun.status }}</span></div>
                <details v-if="activeRun.draft" class="draft-details" :open="activeRun.status === 'awaiting_confirmation'">
                  <summary>核对申请字段</summary>
                  <dl class="draft-summary"><template v-for="(value, key) in activeRun.draft" :key="key"><div v-if="value !== '' && value != null"><dt>{{ draftLabels[key] || key }}</dt><dd>{{ key === 'workflow' ? workflowLabels[value] || value : draftValueLabels[key]?.[value] || value }}</dd></div></template></dl>
                </details>
                <button v-if="activeRun.status === 'awaiting_confirmation'" :disabled="busy" @click="confirmDraft">核对并确认创建工单</button>
                <button v-if="['running','queued'].includes(activeRun.status)" @click="pauseRun">暂停</button>
                <button v-if="['paused','failed'].includes(activeRun.status)" :disabled="busy" @click="resumeRun">恢复任务</button>
                <p v-if="activeRun.ticket_id" class="ticket-receipt">工单号：{{ activeRun.ticket_id }} <button class="text-button" @click="activeView = 'service'">查看办理进度 <AppIcon name="northeast" /></button></p>
                <button :disabled="busy" @click="submitFeedback('good')">有帮助</button>
                <button :disabled="busy" @click="submitFeedback('bad')">需改进</button>
                <button v-for="citation in citations" :key="citation" class="text-button" @click="showCitation(citation)">查看来源 {{ citation.slice(0,10) }}</button>
                <pre v-if="citationText">{{ citationText }}</pre>
              </section>
              <p class="composer-note">申请提交前由你核对确认，审批与资源操作按服务流程办理。</p>
            </div>
          </section>
          <aside v-if="messages.length" class="inspector-desktop" aria-label="请求追踪与运行状态">
            <div class="inspector-heading">
              <AppIcon name="panel" />
              <h2>运行详情</h2>
            </div>
            <RequestInspector :last-response="lastResponse" :last-trace="lastTrace" :busy="busy && pendingAction === 'chat'" :monitor-data="monitorData" @refresh="loadMonitor" />
          </aside>
        </div>
      </section>
      <section v-else-if="activeView === 'knowledge'" class="page page-knowledge">
        <div class="page-heading">
          <div class="heading-copy">
            <span class="kicker">KNOWLEDGE OPERATIONS</span>
            <h1>知识库</h1>
            <p>浏览已发布文档，或输入问题检索相关知识。</p>
          </div>
          <div class="count-display">
            <strong>{{ knowledgeCount }}</strong>
            <span>知识片段</span>
          </div>
        </div>
        <KnowledgeLibrary ref="libraryPanel" :settings="settings" />
        <div class="knowledge-layout" :class="{ 'knowledge-reader': !isSupport }">
          <section class="workspace-card search-workspace">
            <div class="card-heading">
              <div>
                <span class="kicker">RETRIEVAL</span>
                <h2>检索知识</h2>
              </div>
              <AppIcon name="search" />
            </div>
            <form class="search-line" @submit.prevent="performAction('search', searchKnowledge)">
              <input v-model="searchQuery" aria-label="检索问题" placeholder="例如：VPN 错误 691 怎么排查" />
              <button :disabled="busy || !searchQuery.trim()">
                <AppIcon name="search" />
                {{ busy && pendingAction === 'search' ? '检索中' : '搜索' }}
              </button>
            </form>
            <div v-if="searchResults.length" class="result-list">
              <article v-for="(item, index) in searchResults" :key="item.id || index" class="result-item">
                <span class="result-number">{{ String(index + 1).padStart(2, '0') }}</span>
                <div>
                  <div class="result-title">
                    <strong>{{ item.title || '未命名文档' }}</strong>
                    <small>相关度 {{ item.score ?? '—' }}</small>
                  </div>
                  <p>{{ item.content }}</p>
                </div>
              </article>
            </div>
            <div v-else class="workspace-empty">
              <AppIcon name="book" />
              <h3>{{ searched ? '暂未找到相关知识' : '尚未执行检索' }}</h3>
              <p>{{ searched ? '尝试换一个问题；文档目录仍可在上方浏览。' : '已有文档显示在上方。输入 IT 问题，查看相关知识片段。' }}</p>
            </div>
          </section>
          <section v-if="isSupport" class="workspace-card import-workspace">
            <div class="card-heading">
              <div>
                <span class="kicker">INGESTION</span>
                <h2>添加知识</h2>
              </div>
              <AppIcon name="document" />
            </div>
            <form @submit.prevent="performAction('import', submitKnowledge)">
              <label>
                <span>文档标题</span>
                <input v-model="docTitle" required placeholder="例如：VPN 故障处理规范" />
              </label>
              <label>
                <span>知识内容</span>
                <textarea v-model="docContent" required rows="7" placeholder="输入客服规范、产品说明或排障流程"></textarea>
              </label>
              <div class="side-actions">
                <button :disabled="busy || !docTitle.trim() || !docContent.trim()">
                  <AppIcon name="arrow" />
                  {{ busy && pendingAction === 'import' ? '添加中' : '添加文档' }}
                </button>
                <label class="upload-button" :class="{ disabled: busy }">
                  <AppIcon name="upload" />
                  上传文件
                  <input :disabled="busy" type="file" accept=".txt,.md,.json" @change="performAction('upload', handleUpload, $event)" />
                </label>
              </div>
              <p class="form-hint">客服角色可导入 UTF-8 TXT、Markdown、JSON，文件最大 10 MiB，JSON 最多 100 篇；新增材料保存为草稿，请在下方补齐元数据并审核发布。</p>
            </form>
          </section>
        </div>
        <KnowledgeGovernance v-if="isSupport" ref="governancePanel" :settings="settings" @changed="refreshKnowledge" />
        <section v-if="isSupport" class="workspace-card skills-workspace">
          <div class="card-heading">
            <div>
              <span class="kicker">LOADED SKILLS</span>
              <h2>已加载能力</h2>
            </div>
            <button class="text-button" :disabled="busy" @click="performAction('skills', reloadSkillSet)">
              <AppIcon name="refresh" />
              重新加载
            </button>
          </div>
          <div class="skill-table">
            <div v-for="skill in skillsData.skills" :key="skill.name" class="skill-item">
              <span class="skill-dot"></span>
              <strong>{{ skill.name }}</strong>
              <span>{{ skill.description || '业务规范能力' }}</span>
              <small>{{ skill.content_chars || 0 }} chars</small>
            </div>
            <p v-if="!skillsData.skills?.length" class="skills-empty">暂无已加载能力，连接服务后刷新查看。</p>
          </div>
        </section>
      </section>
      <section v-else-if="activeView === 'operations'" class="page">
        <div class="page-heading"><h1>任务与连接</h1><button @click="loadOperations">刷新</button></div>
        <section class="workspace-card"><h2>我的任务</h2><p v-for="run in runList" :key="run.id"><button class="text-button" @click="restoreRun(run.id)">{{ run.message.slice(0,45) }} · {{ run.status }}</button></p></section>
        <section class="workspace-card"><h2>我的长期记忆</h2><pre>{{ formatJson(memoryData) }}</pre></section>
        <section v-if="isSupport" class="workspace-card"><h2>MCP 连接</h2><pre>{{ formatJson(mcpData) }}</pre></section>
        <section v-if="isSupport" class="workspace-card"><h2>模型接入</h2><template v-if="modelInfo?.model"><p>协议：{{ modelInfo.model.protocol }} · 模型：{{ modelInfo.model.model }}</p><p>服务地址：{{ modelInfo.model.base_url }}</p><p>{{ modelInfo.model.configured ? '服务端已配置密钥' : '服务端尚未配置密钥' }}</p><p class="muted">在支持后台配置和切换模型，查看持久化用量与估算费用。密钥不返回浏览器。</p><button class="quiet-button" @click="activeView='admin'">进入支持后台</button></template><p v-else-if="modelInfo?.error" class="muted">{{ modelInfo.error }}</p><p v-else class="muted">正在读取服务端配置状态。</p></section>
      </section>
      <ServiceDesk v-else-if="activeView === 'service'" :settings="settings" :role="currentRole" @compose="message => { activeView = 'chat'; usePrompt(message) }" />
      <SupportAdmin v-else-if="activeView === 'admin' && isSupport" :key="settings.pythonToken" :settings="settings" />
      <section v-else-if="activeView === 'evaluation' && isSupport" class="page page-evaluation">
        <div class="page-heading">
          <div class="heading-copy">
            <span class="kicker">EVALUATION LAB</span>
            <h1>评测中心</h1>
            <p>检索回归验证知识命中；完整质量评测通过隔离数据执行真实对话、业务流程和模型评分。</p>
          </div>
          <select v-model="evaluationKind" :disabled="busy" aria-label="评测类型"><option value="retrieval">知识检索回归</option><option value="quality">完整质量评测（调用配置模型）</option></select>
          <button @click="performAction('evaluation', runEvaluation)" :disabled="busy">
            <AppIcon :name="busy && pendingAction === 'evaluation' ? 'refresh' : 'play'" :class="{ spinning: busy && pendingAction === 'evaluation' }" />
            {{ busy && pendingAction === 'evaluation' ? '评测运行中' : '运行评测' }}
          </button>
        </div>
        <section class="workspace-card"><h2>本人负反馈回归候选</h2><button class="text-button" @click="loadCandidates">加载本人候选</button><label>期望文档 ID<input v-model="expectedDocument" /></label><label>期望章节<input v-model="expectedSection" /></label><p v-for="c in candidates" :key="c.run_id">{{ c.question }} <button :disabled="!expectedDocument || !expectedSection" @click="promoteCandidate(c.run_id)">审核并加入回归</button></p></section>
        <section v-if="evaluationKind === 'quality'" class="workspace-card">
          <h2>自定义质量评测</h2>
          <label>评测集 JSON（可填写 intent_cases、dialog_cases、逐轮 reference/evidence 和断言；留空使用合成开发集）<textarea v-model="evaluationDataset" rows="8" :disabled="busy" aria-label="自定义评测集 JSON"></textarea></label>
          <label>本次总时限（秒，1–3600）<input v-model.number="evaluationTimeout" type="number" min="1" max="3600" :disabled="busy" /></label>
          <button @click="saveEvaluationDataset" :disabled="busy || !evaluationDataset.trim()">保存评测集（客服）</button><p>{{ savedDatasetId ? `已保存：${savedDatasetId}` : '' }}</p>
        </section>
        <section class="workspace-card"><h2>评测任务记录</h2><button @click="loadEvaluationJobs">加载记录（客服）</button><label>恢复时额外增加秒数（0–3600，0 表示只用剩余时间）<input v-model.number="evaluationExtraTime" type="number" min="0" max="3600" /></label><p>恢复会复用已完成结果；无法确认结果的付费步骤不会自动重调。</p><p v-for="job in evaluationJobs" :key="job.job_id">{{ job.options.kind || 'retrieval' }} · {{ job.status }} · {{ job.completed }}/{{ job.total }} · 用时 {{ Math.ceil(job.used_s || 0) }}/{{ job.budget_s }} 秒 <button @click="openEvaluationJob(job)" :disabled="busy">查看结果</button><button v-if="['paused','failed'].includes(job.status)" @click="continueEvaluationJob(job)" :disabled="busy">恢复未完成部分（可能产生模型费用）</button></p></section>
        <div v-if="evalData" class="evaluation-content">
          <section class="workspace-card"><button @click="loadEvaluationRetention">预览评测保留（客服）</button><p>回收需在运维端核验备份后显式执行；已选基线、作者评测集和运行中任务会保留。</p><p v-if="evaluationRetention">当前可回收 {{ evaluationRetention.plans?.length || 0 }} 项，受保护 {{ evaluationRetention.protected?.length || 0 }} 项。</p><details v-if="evaluationRetention"><summary>查看范围与保护原因</summary><pre>{{ formatJson(evaluationRetention) }}</pre></details></section>
          <p v-if="evalData.kind === 'active_service_quality'">{{ evalData.provenance }} · 意图样本 {{ evalData.intent?.samples }} 条 · 对话 {{ evalData.total }} 组。评分错误不会记为有效评分。</p>
          <button v-if="evalData.kind === 'active_service_quality' && evalData.job_id" @click="saveQualityBaseline" :disabled="busy || !evalData.judge_enabled || evalData.evaluation_errors > 0 || !evalData.total || evalData.valid_total !== evalData.total">将当前报告设为人工选定的回归基线（客服）</button>
          <div class="evaluation-summary">
            <div class="score-hero">
              <span>PASS RATE · 通过率</span>
              <strong :class="{ 'no-score': evalData.valid_total === 0 }">{{ evalData.valid_total === 0 ? '无有效评分' : formatPercent(evalData.pass_rate) }}</strong>
              <small>{{ evalData.passed }} / {{ evalData.total }} 个请求通过；其中 {{ evalData.valid_total ?? evalData.total }} 个结果有效</small>
            </div>
            <div>
              <span>通过用例</span>
              <strong>{{ evalData.passed }}</strong>
            </div>
            <div>
              <span>总数 / 评分失败</span>
              <strong>
                {{ evalData.total }}
                <small> / {{ evalData.evaluation_errors ?? 0 }}</small>
              </strong>
            </div>
            <div>
              <span>回归问题</span>
              <strong :class="evalData.regressions?.length ? 'danger' : 'success'">{{ evalData.regressions?.length || 0 }}</strong>
            </div>
          </div>
          <div class="evaluation-layout">
            <section class="workspace-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">QUALITY SCORES</span>
              <h2>{{ evalData.kind === 'active_service_quality' ? '回答质量与业务指标' : '检索指标' }}</h2>
                </div>
              </div>
              <p v-if="!Object.keys(evalData.avg_scores || {}).length" class="muted">没有有效评分，请查看失败原因。</p>
              <div class="score-list">
                <div v-for="(value, key) in evalData.avg_scores" :key="key">
                  <span>{{ key }}</span>
                  <i>
                    <b :style="{ width: `${Math.max(0, Math.min(Number(value) * 100, 100))}%` }"></b>
                  </i>
                  <strong>{{ Number(value).toFixed(2) }}</strong>
                </div>
              </div>
            </section>
            <section class="workspace-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">RECOMMENDATIONS</span>
                  <h2>优化建议</h2>
                </div>
              </div>
              <p v-if="evalData.regression_note" class="muted">{{ evalData.regression_note }}</p>
              <div v-if="evalData.recommendations?.length" class="recommendations">
                <p v-for="(item, index) in evalData.recommendations" :key="index">{{ item }}</p>
              </div>
              <p v-else class="muted">本次评测没有返回额外建议。</p>
              <div v-for="(result, index) in evalData.regressions || []" :key="index" class="evaluation-warning">
                    <strong class="danger">回归问题</strong>
                <p>{{ typeof result === 'string' ? result : formatJson(result) }}</p>
              </div>
              <div v-for="result in (evalData.results || []).filter(item => item.metadata?.judge_failed)" :key="result.test_id" class="evaluation-warning">
                <strong class="danger">{{ result.test_id }}：评分失败</strong>
                <p>{{ result.metadata.judge_error || '模型未返回有效评分' }}</p>
                <small>{{ result.metadata.judge_attempts ?? '—' }} 次尝试</small>
              </div>
            </section>
          </div>
        </div>
        <div v-else class="evaluation-empty">
          <span class="empty-emblem">
            <AppIcon name="chart" />
          </span>
          <h2>{{ busy && pendingAction === 'evaluation' ? '正在检查知识召回。' : '了解知识召回表现' }}</h2>
          <p>{{ busy && pendingAction === 'evaluation' ? '评测需要一些时间，结果返回后会显示在这里。' : '运行一次评测，了解 Agent 的表现与改进空间。' }}</p>
          <div class="evaluation-dimensions">
            <span>召回覆盖</span>
            <span>相关排序</span>
            <span>反馈回归</span>
          </div>
        </div>
      </section>
    </main>
    <dialog ref="inspectorDialog" class="inspector-dialog" @click="closeOnBackdrop" aria-labelledby="inspector-title">
      <div class="drawer-heading">
        <div>
          <AppIcon name="panel" />
          <h2 id="inspector-title">运行详情</h2>
        </div>
        <button class="icon-button" aria-label="关闭运行详情" @click="inspectorDialog.close()">
          <AppIcon name="close" />
        </button>
      </div>
      <RequestInspector :last-response="lastResponse" :last-trace="lastTrace" :busy="busy && pendingAction === 'chat'" :monitor-data="monitorData" @refresh="loadMonitor" />
    </dialog>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  addKnowledge,
  backendMeta,
  createInitialSettings,
  reloadSkills,
  requestChat,
  requestHealth,
  requestKnowledgeStats,
  requestMonitor,
  requestSearch,
  requestToolTrace,
  requestSkills,
  runEvaluation as requestEvaluation,
  saveSettings,
  uploadKnowledge
} from './lib/resolvedesk'
import { deskRequest, consumeEvents } from './lib/resolvedesk'
import { pollEvaluation } from './lib/evaluationJobs'

import AppIcon from './components/AppIcon.vue'
import RequestInspector from './components/RequestInspector.vue'
import ServiceDesk from './components/ServiceDesk.vue'
import KnowledgeGovernance from './components/KnowledgeGovernance.vue'
import KnowledgeLibrary from './components/KnowledgeLibrary.vue'
import { citationText as formatCitation } from './lib/knowledgePublication'

import { createRequestState } from './lib/requestState'
import { verifiedRole, visibleNavigation } from './lib/identity'
import LoginPanel from './components/LoginPanel.vue'
import SupportAdmin from './components/SupportAdmin.vue'
import { readSession, saveSession, clearSession } from './lib/authSession'

const settings = reactive(createInitialSettings())
const governancePanel = ref(null)
const libraryPanel = ref(null)
const requestState = createRequestState(settings)
const activeView = ref('chat')
const identity = ref(null)
const currentRole = computed(() => verifiedRole(settings, identity.value))
const isSupport = computed(() => currentRole.value === 'support')
const modelInfo = ref(null)
const authNotice = ref(''),demoLoginEnabled = ref(false),switchingIdentity = ref(false)
let authenticationEpoch = 0
const messages = ref([])
const activeRun = ref(null), streamText = ref(''), progressText = ref(''), citationText = ref('')
const runList = ref([]), memoryData = ref({}), mcpData = ref({}), candidates = ref([])
const expectedDocument = ref(''), expectedSection = ref('')
const citations = computed(()=>[...new Set((lastResponse.value?.response||'').match(/C-[a-f0-9]{24}/g)||[])])
let activeStreamController
const draft = ref('')
const busy = ref(false)
const healthOk = ref(false)
const healthLabel = ref('未检查')
const statusText = ref('')
const knowledgeCount = ref('-')
const searchQuery = ref('')
const searchResults = ref([])
const docTitle = ref('')
const docContent = ref('')
const messageList = ref(null)
const composerInput = ref(null)
const inspectorDialog = ref(null)
const pendingAction = ref('')
const searched = ref(false)
const views = [
  { id: 'chat', label: '对话', icon: 'chat' },
  { id: 'knowledge', label: '知识库', icon: 'book' },
  { id: 'operations', label: '任务与连接', icon: 'route' },
  { id: 'service', label: '业务服务台', icon: 'route' },
  { id: 'evaluation', label: '评测', icon: 'chart' },
  { id: 'admin', label: '支持后台', icon: 'grid' }
]
const visibleViews = computed(() => visibleNavigation(views, currentRole.value))
const prompts = [
  { label: '账号与权限', icon: 'key', description: '申请资源访问，明确范围和期限', text: '请帮我准备权限申请，先核对可申请资源并询问缺少的信息。' },
  { label: '设备维修', icon: 'monitor', description: '核对本人设备，提交故障与维修', text: '请帮我准备本人设备维修申请，先核对本人资产并询问缺少的信息。' },
  { label: '软件授权', icon: 'grid', description: '查询产品席位，申请使用授权', text: '请帮我准备 IntelliJ IDEA Ultimate 模拟软件授权申请，先查询实时席位，询问我的用途和使用天数。' },
  { label: '网络排障', icon: 'network', description: '依据知识定位连接与访问问题', text: 'VPN 错误 691 怎么排查？请引用知识库。' }
]
const draftLabels = { summary: '申请摘要', description: '用途 / 问题描述', category: '服务类别', priority: '优先级', workflow: '办理流程', product: '软件产品', resource_id: '申请资源', access_level: '访问级别', duration_days: '使用天数', asset_id: '设备编号' }
const draftValueLabels = { category: { software: '软件服务', network: '网络服务', access: '账号权限', hardware: '设备服务', other: '其他服务' }, priority: { low: '较低', normal: '普通', high: '紧急' }, access_level: { read: '只读', write: '读写' } }
const workflowLabels = { access_request: '权限申请', device_repair: '设备维修', license_request: '软件授权', generic: '服务工单' }
const runStatusLabels = { running: '正在处理', queued: '等待处理', awaiting_input: '待补充信息', awaiting_confirmation: '待核对确认', completed: '本轮已完成', paused: '已暂停', failed: '处理失败' }
const currentView = computed(() => views.find(view => view.id === activeView.value))
const monitorData = ref({ agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] })
const skillsData = ref({ count: 0, skills: [], errors: [] })
const lastResponse = ref(null)
const lastTrace = ref(null)
const evalData = ref(null)
const evaluationKind = ref('retrieval')
const evaluationDataset = ref('')
const savedDatasetId = ref('')
const evaluationTimeout = ref(540)
const evaluationExtraTime = ref(0)
const evaluationRetention = ref(null)
const evaluationJobs = ref([])
const toast = ref('')
let toastTimer
let messageSequence = 0

const currentBackend = computed(() => backendMeta(settings.backend, settings))
const docsUrl = computed(() => `${currentBackend.value.baseUrl}/docs`)
const userInitial = computed(() => (settings.userId || 'U').slice(0, 1).toUpperCase())

watch(() => settings.conversationId, persist)
watch(activeView, value=>{if(value==='operations')loadOperations()})
watch([activeView, isSupport], ([view, support]) => { if (view === 'operations' && support) loadModelConfiguration() })
watch(currentRole, role => { if (role !== 'support') { modelInfo.value = null; if (['evaluation','admin'].includes(activeView.value)) activeView.value = 'chat' } })
onMounted(async()=>{
  const saved=readSession(settings.endpoints.python,sessionStorage)
  if(saved)settings.pythonToken=saved.token
  window.addEventListener('resolvedesk:unauthorized',handleUnauthorized)
  await Promise.allSettled([refreshConsole(),loadLoginOptions()])
})
onBeforeUnmount(() => {
  requestState.invalidate()
  activeStreamController?.abort()
  clearTimeout(toastTimer)
  window.removeEventListener('resolvedesk:unauthorized',handleUnauthorized)
})

function persist() { saveSettings(settings) }

async function loadLoginOptions(){try{const options=await deskRequest(settings,'/auth/options');demoLoginEnabled.value=!!options.demo_login_enabled}catch{demoLoginEnabled.value=false}}

function clearIdentityWorkspace(){
  requestState.invalidate();activeStreamController?.abort();identity.value=null;settings.pythonToken='';settings.userId='未登录';settings.conversationId=''
  activeView.value='chat';busy.value=false;messages.value=[];activeRun.value=null;lastResponse.value=null;lastTrace.value=null
  runList.value=[];memoryData.value={};mcpData.value={};candidates.value=[];searchResults.value=[];modelInfo.value=null;citationText.value=''
  monitorData.value={agent_stats:{},tool_stats:{},active_alerts:[],suggestions:[]};skillsData.value={count:0,skills:[],errors:[]}
  evalData.value=null;evaluationJobs.value=[];evaluationRetention.value=null;evaluationDataset.value='';savedDatasetId.value='';docTitle.value='';docContent.value='';draft.value=''
  searched.value=false;searchQuery.value='';knowledgeCount.value='-';healthOk.value=false;persist()
}

async function applySession(session){
  authenticationEpoch+=1;switchingIdentity.value=false
  clearIdentityWorkspace();authNotice.value='';settings.pythonToken=session.token;settings.userId=session.principal.user
  identity.value={...session.principal,token:session.token,endpoint:settings.endpoints.python,backend:'python'}
  saveSession(settings.endpoints.python,session,sessionStorage);persist();await refreshConsole()
}

function handleUnauthorized(event){if(event.detail?.token===settings.pythonToken && event.detail?.endpoint===currentBackend.value.baseUrl){clearSession(settings.endpoints.python,sessionStorage);clearIdentityWorkspace();authNotice.value='登录已过期或被撤销，请重新登录。'}}

async function logoutSession(){
  authenticationEpoch+=1
  switchingIdentity.value=false
  const previous={...settings,endpoints:{...settings.endpoints}}
  clearSession(settings.endpoints.python,sessionStorage);clearIdentityWorkspace();authNotice.value='已退出登录。'
  try{await deskRequest(previous,'/auth/logout','POST')}catch{}
}

async function switchIdentity(role){
  if(!demoLoginEnabled.value || switchingIdentity.value)return
  const epoch=++authenticationEpoch,previous={...settings,endpoints:{...settings.endpoints}}
  switchingIdentity.value=true;clearSession(settings.endpoints.python,sessionStorage);clearIdentityWorkspace()
  deskRequest(previous,'/auth/logout','POST').catch(()=>{})
  try{const session=await deskRequest(settings,'/auth/demo-login','POST',{role});if(epoch===authenticationEpoch)await applySession(session)}
  catch(e){if(epoch===authenticationEpoch)authNotice.value=e.message}
  finally{if(epoch===authenticationEpoch)switchingIdentity.value=false}
}

function performAction(name, action, ...args) {
  if (busy.value) return
  pendingAction.value = name
  return action(...args)
}

function closeOnBackdrop(event) {
  const dialog = event.currentTarget
  if (event.target !== dialog) return
  const rect = dialog.getBoundingClientRect()
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close()
}

async function copyMessage(content) {
  try {
    await navigator.clipboard.writeText(content)
    showToast('回复已复制')
  } catch {
    showToast('复制失败，请手动选择回复文字')
  }
}

function switchBackend(type) {
  if (settings.backend === type) return
  activeStreamController?.abort()
  searched.value = false
  requestState.invalidate()
  busy.value = false
  statusText.value = ''
  knowledgeCount.value = '-'
  monitorData.value = { agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] }
  skillsData.value = { count: 0, skills: [], errors: [] }
  evalData.value = null
  evaluationJobs.value = []
  evaluationRetention.value = null
  savedDatasetId.value = ''
  settings.backend = type
  persist()
  healthOk.value = false
  healthLabel.value = '未检查'
  messages.value = []
  searchResults.value = []
  lastResponse.value = null
  lastTrace.value = null
  refreshConsole()
}

async function refreshConsole() {
  await Promise.allSettled([checkHealth(), loadStats(), loadMonitor(), loadSkills()])
}

async function checkHealth() {
  const request = requestState.begin('health')

  try {
    const data = await requestHealth(request.backend, request.settings)
    let nextIdentity = null
    if(request.settings.pythonToken){const me=await deskRequest(request.settings,'/me');nextIdentity={...me,token:request.settings.pythonToken,endpoint:request.settings.endpoints[request.backend],backend:request.backend}}
    if (!request.isCurrent()) return
    identity.value = nextIdentity
    settings.userId = nextIdentity?.user || '未登录'
    healthOk.value = data.status === 'ok'
    healthLabel.value = data.status || 'ok'
    statusText.value = JSON.stringify(data, null, 2)
  } catch (error) {
    if (!request.isCurrent()) return
    identity.value = null
    // 健康检查只更新状态，保留用户选择的后端。
    healthOk.value = false
    healthLabel.value = '不可用'
    statusText.value = error.message
  }
}

async function loadModelConfiguration() {
  const request = requestState.begin('model-configuration')
  try {
    const result = await deskRequest(request.settings, '/system')
    if (request.isCurrent() && isSupport.value) modelInfo.value = result
  } catch (error) { if (request.isCurrent() && isSupport.value) modelInfo.value = { error: error.message } }
}

async function loadStats() {
  const request = requestState.begin('stats')
  try {
    const data = await requestKnowledgeStats(request.backend, request.settings)
    if (!request.isCurrent()) return
    knowledgeCount.value = data.total_chunks ?? data.totalChunks ?? '-'
  } catch {
    if (request.isCurrent()) knowledgeCount.value = '-'
  }
}

async function refreshKnowledge() {
  await Promise.allSettled([loadStats(), libraryPanel.value?.load()])
}

async function loadMonitor() {
  const request = requestState.begin('monitor')
  try {
    const data = await requestMonitor(request.backend, request.settings)
    if (request.isCurrent()) monitorData.value = { ...data, loaded: true }
  } catch {
    if (request.isCurrent()) monitorData.value = { agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] }
  }
}

async function loadSkills() {
  const request = requestState.begin('skills')
  try {
    const data = await requestSkills(request.backend, request.settings)
    if (request.isCurrent()) skillsData.value = data
  } catch {
    if (request.isCurrent()) skillsData.value = { count: 0, skills: [], errors: [] }
  }
}

async function reloadSkillSet() {
  const request = requestState.begin('action')
  busy.value = true
  try {
    const data = await reloadSkills(request.backend, request.settings)
    if (!request.isCurrent()) return
    skillsData.value = data
    showToast('Skills 已重新加载')
  } catch (error) {
    if (!request.isCurrent()) return
    statusText.value = error.message
    showToast('Skills 加载失败')
  } finally { if (request.isCurrent()) busy.value = false }
}

async function sendMessage() {
  const content = draft.value.trim()
  if (!content || busy.value) return
  messages.value.push({ id: createMessageId(), role: 'user', content })
  draft.value = ''
  lastResponse.value = null
  lastTrace.value = null
  activeRun.value = null; streamText.value = ''; progressText.value = ''; citationText.value = ''
  nextTick(() => messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' }))
  const request = requestState.begin('action')
  busy.value = true
  try {
    activeStreamController = new AbortController()
    const response = await requestChat(request.backend, request.settings, content, {onEvent:handleRunEvent,isCurrent:request.isCurrent,signal:activeStreamController.signal})
    if (!request.isCurrent()) return
    if (response.conversationId) {
      settings.conversationId = response.conversationId
      persist()
    }
    const trace = response.toolTrace
    if (!request.isCurrent()) return
    lastResponse.value = response
    activeRun.value = response.raw
    lastTrace.value = trace
    const meta = [response.intent, response.primaryAgent || response.agentType, response.knowledgeUsed ? 'RAG' : '', response.escalated ? '需人工核验' : ''].filter(Boolean).join(' · ')
    messages.value.push({ id: createMessageId(), role: 'assistant', content: response.response, meta, trace: lastTrace.value?.trace || null })
    await loadMonitor()
  } catch (error) {
    if (!request.isCurrent()) return
    messages.value.push({ id: createMessageId(), role: 'assistant', content: error.message, meta: '请求失败', error: true })
  } finally {
    if (!request.isCurrent()) return
    busy.value = false
    await nextTick()
    messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' })
  }
}

function usePrompt(prompt) { draft.value = prompt; nextTick(() => composerInput.value?.focus()) }

function clearConversation() {
  activeStreamController?.abort()
  requestState.invalidate()
  busy.value = false
  messages.value = []
  lastResponse.value = null
  lastTrace.value = null
  activeRun.value = null
  streamText.value = ''; progressText.value = ''; citationText.value = ''
  settings.conversationId = ''
  persist()
}

async function searchKnowledge() {
  if (!searchQuery.value.trim() || busy.value) return
  const request = requestState.begin('action')
  busy.value = true
  try {
    const data = await requestSearch(request.backend, request.settings, searchQuery.value, 5)
    if (!request.isCurrent()) return
    searchResults.value = data.results || []
    searched.value = true
    showToast(`检索完成，返回 ${searchResults.value.length} 条结果`)
  } catch (error) {
    if (!request.isCurrent()) return
    statusText.value = error.message
    showToast('检索失败，请检查连接')
  } finally { if (request.isCurrent()) busy.value = false }
}

async function submitKnowledge() {
  if (!docTitle.value.trim() || !docContent.value.trim() || busy.value) return
  const request = requestState.begin('action')
  busy.value = true
  try {
    const data = await addKnowledge(request.backend, request.settings, [{ title: docTitle.value.trim(), content: docContent.value.trim() }])
    if (!request.isCurrent()) return
    statusText.value = JSON.stringify(data, null, 2)
    await loadStats()
    if (!request.isCurrent()) return
    await governancePanel.value?.load()
    if (!request.isCurrent()) return
    showToast('文档已保存为待审核草稿')
  } catch (error) {
    if (!request.isCurrent()) return
    statusText.value = error.message
    showToast('文档导入失败')
  } finally { if (request.isCurrent()) busy.value = false }
}

async function handleUpload(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file || busy.value) return
  if (file.size > 10*1024*1024) { showToast('文件不能超过 10 MiB'); return }
  const request = requestState.begin('action')
  busy.value = true
  try {
    const data = await uploadKnowledge(request.backend, request.settings, file)
    if (!request.isCurrent()) return
    statusText.value = JSON.stringify(data, null, 2)
    await loadStats()
    if (!request.isCurrent()) return
    await governancePanel.value?.load()
    if (!request.isCurrent()) return
    showToast(`${file.name} 已保存为待审核草稿`)
  } catch (error) {
    if (!request.isCurrent()) return
    statusText.value = error.message
    showToast('文件导入失败')
  } finally { if (request.isCurrent()) busy.value = false }
}

async function runEvaluation() {
  const request = requestState.begin('action')
  busy.value = true
  try {
    const options = { kind: evaluationKind.value, timeout_s: evaluationTimeout.value }
    if (evaluationKind.value === 'quality' && evaluationDataset.value.trim()) {
      const dataset = JSON.parse(evaluationDataset.value)
      options.intent_cases = dataset.intent_cases || []
      options.dialog_cases = dataset.dialog_cases || []
      options.assets = dataset.assets || []
      options.limit = Math.max(1, options.dialog_cases.length)
    }
    const data = await requestEvaluation(request.backend, request.settings, options, {
      isCurrent: request.isCurrent,
      onProgress: job => {
        if (request.isCurrent()) statusText.value = `评测任务 ${job.job_id}：${job.status === 'queued' ? '排队中' : '运行中'}，已完成 ${job.completed}/${job.total} 组`
      }
    })
    if (!request.isCurrent()) return
    evalData.value = data
    showToast(data.status === 'failed' ? '评测执行失败，请查看结果' : data.status === 'partial' ? '评测部分完成，请查看失败原因' : '评测完成')
  } catch (error) {
    if (!request.isCurrent()) return
    statusText.value = error.message
    showToast('评测运行失败')
  } finally { if (request.isCurrent()) busy.value = false }
}

async function saveQualityBaseline() {
  const request = requestState.begin('baseline')
  try {
    await deskRequest(request.settings, `/eval/jobs/${evalData.value.job_id}/baseline`, 'POST')
    if (request.isCurrent()) showToast('已保存人工选定的质量基线')
  } catch (error) { if (request.isCurrent()) showToast(error.message) }
}

async function saveEvaluationDataset() {
  const request = requestState.begin('evaluation-dataset')
  try {
    const data = await deskRequest(request.settings, '/eval/datasets', 'POST', JSON.parse(evaluationDataset.value))
    if (request.isCurrent()) { savedDatasetId.value = data.id; showToast('评测集已保存') }
  } catch (error) { if (request.isCurrent()) showToast(error.message) }
}

async function loadEvaluationJobs() {
  const request = requestState.begin('evaluation-jobs')
  try { const data = await deskRequest(request.settings, '/eval/jobs'); if (request.isCurrent()) evaluationJobs.value = data }
  catch (error) { if (request.isCurrent()) showToast(error.message) }
}

async function loadEvaluationRetention() {
  const request = requestState.begin('evaluation-retention')
  try {
    const data = await deskRequest(request.settings, '/eval/retention')
    if (request.isCurrent()) evaluationRetention.value = data
  } catch (error) { if (request.isCurrent()) showToast(error.message) }
}

async function openEvaluationJob(job) {
  const request = requestState.begin('evaluation-result')
  try {
    const data = await deskRequest(request.settings, `/eval/jobs/${job.job_id}`)
    if (request.isCurrent()) { evalData.value = data.result ? { ...data.result, job_id: job.job_id } : null; statusText.value = `任务 ${job.job_id}：${data.status}` }
  } catch (error) { if (request.isCurrent()) showToast(error.message) }
}

async function continueEvaluationJob(job) {
  if (busy.value) return
  const request = requestState.begin('action'); busy.value = true
  try {
    const resumed = await deskRequest(request.settings, `/eval/jobs/${job.job_id}/resume`, 'POST', { extra_seconds: evaluationExtraTime.value })
    const report = await pollEvaluation(resumed, id => deskRequest(request.settings, `/eval/jobs/${id}`), { isCurrent: request.isCurrent, onProgress: state => { if (request.isCurrent()) statusText.value = `任务 ${state.job_id}：${state.completed}/${state.total}` } })
    if (request.isCurrent()) { evalData.value = { ...report, job_id: job.job_id }; await loadEvaluationJobs() }
  } catch (error) { if (request.isCurrent()) showToast(error.message) }
  finally { if (request.isCurrent()) busy.value = false }
}

async function loadToolTrace(requestId, request = requestState.begin('trace')) {
  try {
    return await requestToolTrace(request.backend, request.settings, requestId)
  } catch {
    return null
  }
}

function formatPercent(value) {
  const number = Number(value || 0)
  return `${(number <= 1 ? number * 100 : number).toFixed(1)}%`
}

function formatJson(value) {
  try {
    return JSON.stringify(value ?? {}, null, 2)
  } catch {
    return String(value ?? '')
  }
}

function createMessageId() {
  messageSequence += 1
  return `message-${Date.now()}-${messageSequence}`
}

function showToast(message) {
  toast.value = message
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = '' }, 2600)
}

function handleRunEvent(event) {
  if(event.kind==='run'){activeRun.value=event.payload;settings.conversationId=event.payload.id}
  if(event.kind==='turn_start')streamText.value=''
  if(event.kind==='answer_delta')streamText.value+=event.payload.text
  const labels={retrieval:'检索知识',routing:'分配专业角色',context:'准备记忆与业务规则',model_step:'处理工具结果',awaiting_input:'等待补充信息',awaiting_confirmation:'等待确认草稿',completed:'完成',paused:'已暂停'}
  if(labels[event.kind])progressText.value=labels[event.kind]
  if(event.kind==='awaiting_confirmation'&&activeRun.value)activeRun.value.status='awaiting_confirmation'
}
async function confirmDraft(){
  if (!activeRun.value || busy.value) return
  const run = { ...activeRun.value }, request = requestState.begin('conversation')
  const current = () => request.isCurrent() && activeRun.value?.id === run.id
  busy.value = true; pendingAction.value = 'confirm'
  try {
    const ticket = await deskRequest(request.settings, `/runs/${run.id}/confirm`, 'POST', { approval: run.approval })
    if (!current()) return
    const saved = await deskRequest(request.settings, `/runs/${run.id}`)
    if (!current()) return
    activeRun.value = saved
    messages.value.push({ id: createMessageId(), role: 'assistant', content: `工单已创建：${ticket.id}` })
    showToast('工单创建成功')
  } catch (error) { if (current()) showToast(error.message) }
  finally { if (current()) busy.value = false }
}
async function pauseRun(){
  if (!activeRun.value) return
  const id = activeRun.value.id
  activeStreamController?.abort(); requestState.invalidate()
  const request = requestState.begin('conversation')
  const current = () => request.isCurrent() && activeRun.value?.id === id
  busy.value = true; pendingAction.value = 'pause'
  try { const saved = await deskRequest(request.settings, `/runs/${id}/pause`, 'POST'); if (current()) activeRun.value = saved }
  catch (error) { if (current()) showToast(error.message) }
  finally { if (current()) busy.value = false }
}
async function resumeRun(){
  if (!activeRun.value || busy.value) return
  const id = activeRun.value.id, request = requestState.begin('conversation')
  const current = () => request.isCurrent() && activeRun.value?.id === id
  activeStreamController?.abort()
  const controller = new AbortController(); activeStreamController = controller
  busy.value = true; pendingAction.value = 'chat'; streamText.value = ''
  try {
    const resumed = await deskRequest(request.settings, `/runs/${id}/resume`, 'POST', undefined, controller.signal)
    if (!current()) return
    activeRun.value = resumed
    const query = new URLSearchParams({ after: String(resumed.event_cursor || 0) })
    if (resumed.turn_key) query.set('turn_key', resumed.turn_key)
    const response = await fetch(backendMeta('python', request.settings).baseUrl + `/runs/${id}/events?${query}`, { headers: { Authorization: `Bearer ${request.settings.pythonToken}` }, signal: controller.signal })
    if (!current()) { await response.body?.cancel(); return }
    await consumeEvents(response, event => { if (current()) handleRunEvent(event) }, current)
    if (current()) await restoreRun(id)
  } catch (error) { if (current() && error.name !== 'AbortError') showToast(error.message) }
  finally { if (current()) busy.value = false; if (activeStreamController === controller) activeStreamController = undefined }
}
async function restoreRun(id){
  activeStreamController?.abort(); requestState.invalidate()
  const request = requestState.begin('conversation')
  busy.value = true; pendingAction.value = 'restore'
  try {
    const run = await deskRequest(request.settings, `/runs/${id}`)
    if (!request.isCurrent()) return
    const history = await deskRequest(request.settings, `/runs/${id}/messages`)
    if (!request.isCurrent()) return
    let trace = null
    try { trace = await requestToolTrace('python', request.settings, id) } catch { /* History remains readable during trace outages. */ }
    if (!request.isCurrent()) return
    activeRun.value = run; settings.conversationId = id
    messages.value = history.map(message => ({ ...message, id: createMessageId() }))
    if (run.ticket_id) messages.value.push({ id: createMessageId(), role: 'assistant', content: run.answer })
    lastResponse.value = { response: run.answer, raw: run }; lastTrace.value = trace; activeView.value = 'chat'
  } catch (error) { if (request.isCurrent()) showToast(error.message) }
  finally { if (request.isCurrent()) busy.value = false }
}
async function submitFeedback(rating){
  if (!activeRun.value) return
  const id = activeRun.value.id, request = requestState.begin('feedback')
  const current = () => request.isCurrent() && activeRun.value?.id === id
  try { await deskRequest(request.settings, `/runs/${id}/feedback`, 'POST', { rating, note: '' }); if (current()) showToast('反馈已保存') }
  catch (error) { if (current()) showToast(error.message) }
}
async function showCitation(citation){const r=requestState.begin('citation');try{const source=await deskRequest(r.settings,`/knowledge/citations/${citation}`);if(r.isCurrent())citationText.value=formatCitation(source)}catch(e){if(r.isCurrent())showToast(e.message)}}
async function loadOperations(){const r=requestState.begin('operations');try{const data=await Promise.all([deskRequest(r.settings,'/runs'),deskRequest(r.settings,'/memory'),isSupport.value?deskRequest(r.settings,'/mcp'):Promise.resolve({})]);if(r.isCurrent())[runList.value,memoryData.value,mcpData.value]=data}catch(e){if(r.isCurrent())showToast(e.message)}}
async function loadCandidates(){const r=requestState.begin('candidates');try{const data=await deskRequest(r.settings,'/evaluation/candidates');if(r.isCurrent())candidates.value=data}catch(e){if(r.isCurrent())showToast(e.message)}}
async function promoteCandidate(id){try{await deskRequest(settings,`/evaluation/candidates/${id}/promote`,'POST',{document:expectedDocument.value,section:expectedSection.value});showToast('已加入回归集')}catch(e){showToast(e.message)}}
</script>
