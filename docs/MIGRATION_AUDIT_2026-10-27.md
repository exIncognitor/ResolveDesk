# EchoMind → ResolveDesk 技术迁移总审计

审计日期：2026-10-27。被审计代码基线：`9ffcb2dbd326088f3b2ad1466954c56bad725fe8`。

**结论：当前完成了本地 IT 服务功能闭环，但没有完整等价迁移原版 Python 的全部技术行为。此前“完整质量评测、监控已补齐”的表述过宽，应改为“接入了新的评测、监控链路，其中若干原有子能力尚未对齐”。**

本文件固定比较范围、所有已识别差异及完成标准。后续开发按 G01–G13 逐项验收；新功能建议单独列为扩展，不继续混入迁移清单。不能把文件存在、单元测试通过、当前入口接入、实际集成验收四种证据混为一谈。

## 1. 范围与证据

- 原版：本机随包 `EchoMind/` Python 后端的全部 21 个运行模块、`api/main.py` 的全部 17 个装饰器 HTTP 接口、MCP 挂载、CLI、配套 Vue 前端及部署配置。排除依赖、data、日志、history 和文档绘图辅助脚本。
- 原始压缩包：`D:/desktop/实习/echomind/附件/EchoMind所有代码_详细文档_简历.zip`。
- 压缩包 SHA-256：`5a9f6701014df62a25e6fd5fba4010c172f6cbc7f5c3ddda082bee77b8faf8b9`。21 个运行模块均与包内对应文件逐字节一致。
- 当前：`resolvedesk.app:app`、它实际使用的组件与 Vue 适配器。`api.main:app` 是保留的旧入口，不计作新主入口已迁移。
- 原包 Java 是独立实现，按用户“以 Python 为主”的选择单列。不能将 Python 对比结论泛化为整个 Python+Java 原包。
- 原版的代码机制不等于已在生产验证；本审计没有替原版补做线上性能证明，也不调用付费模型。
- 当前测试与真实调用证据沿用 `reports/it-service-delivery-20261027.json` 等已保存报告；本轮不重复运行未变化的全量测试。

状态含义：**已接入**=当前入口确实使用；**替代**=换了实现，需要按功能比较；**部分**=保留或接入一部分但行为不等价；**未接入**=原行为没有进入当前主链路；**保留未验**=旧实现/底层组件存在，但不算新入口完成；**范围选择**=明确限制，不自动要求恢复。

表中 O 表示原包 Python 根目录，R 表示当前 ResolveDesk 根目录。代码位置用于定位机制，不意味着每一行都经过本轮动态测试。完整文件哈希和接口清单见 `reports/migration-audit-20261027.json`。

## 2. 完整能力对照

### 意图识别

| ID | 原技术行为 | 当前状态 | 实际差异或保留能力 | 关键证据 | 对齐包 |
| --- | --- | --- | --- | --- | --- |
| I01 | LLM + Embedding + Pattern 融合结果决定意图及路由 | 部分 | recognizer 仍运行，但业务意图改由 `classify(message)` 决定；融合结果没有继续决定最终 IT 主路由 | O core/intent_recognizer.py:174；R resolvedesk/services.py:156、business.py:35 | G01 |
| I02 | 细粒度意图、融合来源分数、置信度与意图向量来源可观测 | 部分 | 旧组件仍返回细粒度结果，当前对外 IT 域标签与固定 0.9 路由置信度替代了原输出；未完整回传来源分数 | O api/main.py:295；R resolvedesk/services.py:179、frontend/src/lib/resolvedesk.js:67 | G01 |
| I03 | 低置信度 OTHER 确定性澄清、CRITICAL/人工意图优先升级 | 部分 | 当前按“人工/泄露/大面积”等规则升级，主链路不调用原 `_needs_clarification` 和 `_route_decision`，未覆盖原紧急度/置信度分支 | O agents/agent_orchestrator.py:856、1002；R resolvedesk/services.py:156 | G01 |
| I04 | 意图向量 BGE-M3 在线/本地切换、模板缓存、异常冷却 | 保留未验 | 底层仍被 recognizer 使用；本地示例与 CI 默认 local，远端意图向量未验收；知识检索 E5 不等于意图向量已迁移成 E5 | O/R core/intent_embedding.py；R .env.example | G01 |
| I05 | 意图缓存与实体/紧急度提取 | 已接入 | recognizer 的缓存、实体和紧急度仍执行；实体沿用原领域结构，缺少 IT 领域的统一语义映射 | O/R core/intent_recognizer.py:174；R resolvedesk/services.py:156 | G01、G02 |

### Agent 编排与专业工具

| ID | 原技术行为 | 当前状态 | 实际差异或保留能力 | 关键证据 | 对齐包 |
| --- | --- | --- | --- | --- | --- |
| A01 | General/Technical/Billing/Escalation 四角色与动态 Skills | 已接入 | 四角色保留并改写职责；billing 是软件授权的兼容 ID | R resolvedesk/services.py:107；skills/ | — |
| A02 | 主辅角色实际并行执行 | 已接入 | 复合请求确实 `gather(_execute(...))`，主 DeskAgent 再综合专业意见，不是只改提示词 | R resolvedesk/services.py:183；reports/integrated-real-20261026T113225Z.json | — |
| A03 | 同类实例按在线表现选优、冷却与恢复探测 | 部分 | 原 `_best_agent` 只在复合请求执行旧专业节点时运行；普通请求由单个 DeskAgent 处理，不走同样的实例选优和探测路径 | O agents/agent_orchestrator.py:1193；R resolvedesk/agent.py:152、services.py:183 | G03 |
| A04 | 专业节点调用失败立即 General 回退、保留原失败轨迹 | 部分 | 复合节点继承 `_execute` 回退；普通请求主循环异常后直接 failed，可显式恢复，但没有等价的本轮专业失败回退 | O agents/agent_orchestrator.py:1213；R resolvedesk/agent.py:121 | G03 |
| A05 | 结构化人工升级状态与交接摘要 | 部分 | Escalation 类保留；单角色请求只注入角色职责，未完整执行原升级节点，也没有等价的 `escalated` 等响应契约；真实人工队列原版也没有 | O agents/agent_orchestrator.py:616、856；R resolvedesk/services.py:156、agent.py:152 | G03 |
| A06 | 合成器保留 partial/failed、实际执行角色及专业节点轨迹 | 部分 | 由新主 Agent 合成可视为替代，但专业意见包只保留请求角色、success 和文本，丢失 actual agent_type、status、escalate、tool_traces 等信息 | O agents/agent_orchestrator.py:933；R resolvedesk/services.py:183 | G03、G10 |
| A07 | 各专业节点均能自主调用共享 RAG 与获准 MCP 工具 | 未接入 | 新服务没有原 `_refresh_agent_tools/set_shared_tools` 接线；专业节点收到预检索上下文，但不能各自自主检索或调用已注册 MCP。主 DeskAgent 可以调用这些工具 | O api/main.py:219；R resolvedesk/services.py:107、183、227 | G04 |
| A08 | 角色内置工具与领域职责一致 | 部分 | 复合节点仍有旧 `general_tools/technical_tools/billing_tools`；Billing 的订单/金额核验工具未改成软件席位/授权核验；主 DeskAgent 也没有直接暴露整套原角色工具 | O/R agents/tools.py；R resolvedesk/agent.py 的 TOOLS、services.py:107 | G04 |

### 多轮状态与分层记忆

| ID | 原技术行为 | 当前状态 | 实际差异或保留能力 | 关键证据 | 对齐包 |
| --- | --- | --- | --- | --- | --- |
| M01 | 近期对话、摘要、长期历史与用户画像联合注入 | 替代 | 用 SQLite + E5 保存并注入，基本分层结构存在；后续子项不全部等价 | O memory/conversation_memory.py:285；R resolvedesk/services.py:33、agent.py:170 | — |
| M02 | 压缩旧消息、合并历史摘要，压缩后仍保留可追溯原文 | 部分 | 当前有带旧摘要的同步压缩，保留 SQLite 原始消息；未复用原“压缩段归档、同会话优先召回”的组合机制 | O memory/conversation_memory.py:322、545；R resolvedesk/services.py:44 | G05 |
| M03 | 多段情景记忆，同会话优先再跨会话补足 | 部分 | 当前每个 run 一条 episode，检索排除当前 run，仅扫描最近 200 条并按相似度选取；粒度和召回顺序不同 | O memory/conversation_memory.py:394、418；R resolvedesk/services.py:44、79 | G05 |
| M04 | LLM 提炼并更新 preferences、entities 等丰富画像 | 部分 | 当前仅正则提取用户显式 OS/IDE，没有原偏好、产品、问题实体等画像更新 | O memory/conversation_memory.py:231；R resolvedesk/services.py:79 | G05 |
| M05 | 有界后台维护队列、按用户合并待更新任务、更新互斥 | 未接入 | 原 ProfileUpdater/用户锁保留，新入口未启用；摘要在请求内生成，remember 也是请求内 await | O core/concurrency.py:75、api/main.py:202；R resolvedesk/services.py:44、79 | G05 |
| M06 | 维护失败保留已有记忆并尽量不阻断当前聊天 | 部分 | remember 错误会记事件且不覆盖业务结果；但 context 摘要调用异常或截断会向上抛出，导致当前 run failed，不等价于原后台维护失败隔离 | O memory/conversation_memory.py:322、core/concurrency.py:102；R resolvedesk/services.py:44、agent.py:121 | G05 |
| M07 | 结构化实体跨轮继承、字段来源 request_id、换任务/换领域清旧字段 | 部分 | 当前只对工单 draft 合并字段；未调用原 merge_state，没有完整字段来源及新任务切换清理机制 | O core/conversation_state.py:5、agents/agent_orchestrator.py:856；R resolvedesk/store.py:194、agent.py:251 | G02 |

### RAG 与工具可靠性

| ID | 原技术行为 | 当前状态 | 实际差异或保留能力 | 关键证据 | 对齐包 |
| --- | --- | --- | --- | --- | --- |
| R01 | 文档切片、向量召回、重导入 | 替代 | 当前标题/原文跨度分块、E5 向量、中文 BM25/RRF、版本化重导入已接入；不必为“迁移”退回原分块算法 | O tooling/knowledge_base.py；R resolvedesk/retrieval.py | — |
| R02 | 多查询改写、并行召回、去重、LLM 重排 | 已接入 | 当前改写与 Flash 重排可回退，融合算法和参数不同，但核心功能保留 | O tooling/tool_manager.py:310、340；R resolvedesk/services.py:192 | — |
| R03 | 模型按需选择 RAG，专业节点共享检索工具 | 部分 | 当前主入口先强制检索，同时允许主模型再检索；专业节点自主检索缺失见 A07。这是不同执行策略，不能说原执行流原样迁移 | O README.md、agents/tools.py:179；R resolvedesk/agent.py:152 | G04 |
| R04 | 知识查询/工具结果 TTL 缓存与重导入失效 | 未接入 | 新 HybridKnowledge/search 没有原 RAG 结果缓存链；MCP 调用也显式 use_cache=False。底层缓存类存在不等于当前在用 | O tooling/tool_manager.py:176、451、api/main.py:552；R resolvedesk/services.py:192、230 | G06 |
| R05 | 知识工具的独立超时、熔断、降级结果 | 部分 | 当前改写/重排失败与启动时无向量降级已有；底层检索异常没有通过原 knowledge_search 工具的 timeout/breaker/fallback 统一处理 | O api/main.py:141、tooling/tool_manager.py:176；R resolvedesk/services.py:192、agent.py:121 | G06 |
| R06 | 工具 Schema、Agent 白名单、结果边界 | 已接入 | 当前本地工具做 JSON Schema 与 Pydantic 检查，外部 MCP 保留白名单和结果截断机制；共享给专业节点的部分仍见 A07 | O agents/tools.py、tooling/mcp_client.py；R resolvedesk/agent.py、services.py:227 | — |
| R07 | 工具熔断及恢复 | 部分 | MCP 调用仍通过原 manager，含 timeout/breaker；原生知识/本地业务工具没有统一进入该管理层。不能把它概括为全部工具均采用原熔断策略 | O tooling/tool_manager.py:72、176；R resolvedesk/services.py:230、agent.py:238 | G06 |
| R08 | 服务端批量 JSON 导入、10MB multipart 上传 | 部分 | 当前服务端单篇 JSON 文档接口；Vue 在浏览器解析文件后逐篇提交，限制 300KB/最多10篇；原接口与大小/批处理语义没有等价迁移 | O api/main.py:552、585；R resolvedesk/app.py:275、frontend/src/lib/resolvedesk.js:20 | G11 |

### MCP

| ID | 原技术行为 | 当前状态 | 实际差异或保留能力 | 关键证据 | 对齐包 |
| --- | --- | --- | --- | --- | --- |
| C01 | 标准 SDK stdio 客户端、发现与调用 | 已接入 | 当前真实连接本地 support_checklist，只读；有集成证据 | R resolvedesk/services.py:139、mcp_support.py；tests/test_integrated_services.py | — |
| C02 | Streamable HTTP、外部身份头、配置化多服务 | 部分 | SDK 仍支持，构造函数也能程序传参；正常启动不加载 MCP_SERVERS_CONFIG，只硬编码本地 stdio；外部 HTTP 生产接线未验收 | O tooling/mcp_client.py:28、62、149、api/main.py:170；R resolvedesk/services.py:139 | G07 |
| C03 | 工具列表变化刷新、掉线重连 | 已接入 | StandardMCPClient 引擎机制保留；当前默认仅本地 stdio，远端服务及专业节点接线不包含在该完成结论中 | O/R tooling/mcp_client.py:149；R resolvedesk/services.py:149 | G07 |
| C04 | 自身作为 HTTP MCP 服务端暴露知识工具和 Resource | 未接入 | tooling/mcp_server.py 只在旧 api.main 挂载；当前 GET /mcp 是状态 JSON，不是 MCP 协议服务 | O tooling/mcp_server.py、api/main.py:762；R resolvedesk/app.py:303 | G07 |
| C05 | 不调用模型的受控 MCP 调试接口、意图向量连接状态接口 | 未接入 | 当前仅 MCP 状态和检索 embedding 状态；没有原 MCP call 调试及意图 embedding 的 provider/cooldown 状态契约 | O api/main.py:732、747；R resolvedesk/app.py:184、303 | G07、G11 |
| C06 | 非只读 MCP 的未知结果防重放保护 | 范围选择 | 当前明确禁止非只读 MCP，业务写入只走本地确认事务。原泛化写工具及 before_unsafe_tool 路径未迁移；不应为凑功能直接取消确认限制 | O api/main.py:410；R resolvedesk/services.py:144、business.py、store.py:227 | G07（可明确排除） |

### 存储、并发与请求可靠性

| ID | 原技术行为 | 当前状态 | 实际差异或保留能力 | 关键证据 | 对齐包 |
| --- | --- | --- | --- | --- | --- |
| L01 | 身份与读写权限控制 | 替代 | 当前强制令牌绑定租户/用户/角色，支持知识权限预过滤；不同于原可选 user/admin 配置，但基本功能已接入并扩展 | O core/access.py；R resolvedesk/app.py:131、retrieval.py:172 | — |
| L02 | 每轮消息 client_request_id 回执、重复消息重放同结果 | 部分 | create_run 和 confirm 有幂等；POST /runs/{id}/reply 没有独立 request_key 和逐轮回执，不能声称每个续聊请求都等价防重放 | O api/main.py:357、memory/conversation_memory.py:148；R resolvedesk/app.py:216、store.py:194 | G12 |
| L03 | 本轮业务写入一致性 | 替代 | 当前运行状态、确认建单、业务状态、审计在 SQLite 事务内提交；支持旧草稿失效、版本检查和重复确认复用 | O memory/conversation_memory.py:155；R resolvedesk/store.py:205、227、business.py:167 | — |
| L04 | Redis 工作记忆、Chroma 情景/画像/知识服务 | 保留未验 | 原路径仍在旧入口；新入口用 SQLite + 本地 E5 精确向量检索，属于替代，不代表接入了原服务化存储或证明了大规模性能 | O memory/conversation_memory.py、tooling/knowledge_base.py；R resolvedesk/store.py、retrieval.py | G12（架构决策） |
| L05 | 跨进程会话租约、续租、失租取消写入 | 保留未验 | 当前 FileLock 限单进程，SQLite claim 防同 run 重复执行；不允许多个服务 worker，未实现等价分布式会话租约 | O core/concurrency.py:37；R resolvedesk/app.py:85、store.py:143 | G12（架构决策） |

### 质量评测

| ID | 原技术行为 | 当前状态 | 实际差异或保留能力 | 关键证据 | 对齐包 |
| --- | --- | --- | --- | --- | --- |
| E01 | 异步任务、进度、结果查询 | 替代 | 当前任务持久化、重启暂停、显式重新执行，较原进程内任务更完整 | O evaluation/jobs.py；R resolvedesk/evaluations.py | — |
| E02 | 用真实三路 recognizer 计算 Accuracy/Macro-F1 | 部分 | 当前 intent_report 直接测 classify() 规则，28/28 不能证明三路识别器准确率；原 IntentEvaluator 没进入当前评测入口 | O evaluation/evaluator.py:286；R resolvedesk/quality.py:24 | G08 |
| E03 | 四维 Judge、Few-shot、评分失败不伪造结果 | 已接入 | 当前复用 LLMJudge 和五条校准例，记录评分错误；用同一 Flash 生成和评分仍有偏差 | O/R evaluation/evaluator.py:119、few_shot.py；R resolvedesk/quality.py:74 | — |
| E04 | 多轮对话每一轮都评分与保留结果 | 部分 | 当前会执行多轮，但每组只对最后一个 run.answer 调一次 Judge，早期澄清回复未逐轮评分 | O evaluation/evaluator.py:505；R resolvedesk/quality.py:93、136 | G08 |
| E05 | API 自定义意图/对话集、参考资料、最多50轮等配置 | 未接入 | 当前接口仅 kind/limit/use_judge，固定 demo 租户与6组对话；没有原自定义 intent_cases/dialog_cases 契约 | O api/main.py:525–550；R resolvedesk/app.py:75、quality.py:42 | G08 |
| E06 | 任意期望意图/工具/实体的通用确定性断言 | 部分 | 当前有角色、引用、prepare_ticket、专用业务字段与最终状态断言，但未迁移原泛化 expected_intent/required_tools/expected_entities 配置机制 | O evaluation/evaluator.py:505；R resolvedesk/quality.py:105 | G08 |
| E07 | 同 suite 基线回归 | 替代 | 当前人工选择有效基线，相同语料、知识、Judge 配置才比较，相对下降5%检测；不需要复制原自动基线文件行为 | O evaluation/evaluator.py:379、609；R resolvedesk/evaluations.py:62、72 | — |
| E08 | 评测与线上状态隔离 | 替代 | 当前经隔离 HTTP 应用和数据库，业务状态不写主库；原是独立路由/工具状态，知识和外部连接部分共享，隔离方式不同 | O api/main.py:187；R resolvedesk/quality.py:42 | — |

### 监控与轨迹

| ID | 原技术行为 | 当前状态 | 实际差异或保留能力 | 关键证据 | 对齐包 |
| --- | --- | --- | --- | --- | --- |
| O01 | 角色/实例/请求/工具的成功率与耗时 | 部分 | 当前有持久统计，但主请求和专业节点会记在同一角色名下；专业节点统一使用并行批次总耗时，且没有保留实际回退角色与 partial 状态，归因口径不等价 | O monitor/performance_monitor.py:206；R resolvedesk/services.py:183、agent.py:138、monitoring.py:38 | G09 |
| O02 | 滑动窗口 Z-score 异常检测 | 未接入 | 原 AnomalyDetector 在旧监控中；新监控只做固定阈值检测 | O monitor/performance_monitor.py:61；R resolvedesk/monitoring.py:38 | G09 |
| O03 | Prometheus 角色/聊天耗时 Histogram、成功率 Gauge、请求 Counter | 部分 | 当前有鉴权文本指标和窗口 P95，但未完整保留原延迟直方图桶、按请求结果/实例的口径；P95 数值不能替代可聚合 Histogram | O monitor/performance_monitor.py:151；R resolvedesk/monitoring.py:119 | G09 |
| O04 | 工具连续失败、熔断状态、结构化可操作建议 | 部分 | 这些数据仍存在 manager.get_stats，但新 /monitor 没接入；主要只暴露自身记录的成功率/耗时与简单建议 | O tooling/tool_manager.py:508、monitor/performance_monitor.py:239；R resolvedesk/monitoring.py:38 | G09 |
| O05 | 近期成功率和延迟共同参与路由降权、恢复探测 | 部分 | 当前失败告警会做固定0.9角色降权，延迟告警不参与降权；旧连续权重与实例恢复机制仅部分作用于旧专业节点 | O monitor/performance_monitor.py:268、agents/agent_orchestrator.py:90；R resolvedesk/monitoring.py:74、services.py:156 | G03、G09 |
| O06 | Webhook 告警发送 | 替代 | 当前有持久 outbox、触发/恢复、最多3次重试及幂等ID，本地真实 HTTP 已验收；没有配置外部企业通知地址 | O monitor/performance_monitor.py:322；R resolvedesk/monitoring.py:80；reports/it-service-acceptance-20261027T024027Z.json | G13（外部配置） |
| O07 | 工具输入/输出摘要、单次耗时、错误、缓存、回退、MCP来源与最近轨迹列表 | 部分 | 当前主工具只记简化 tool_result，trace 返回 input={}；未合入专业节点完整 tool_traces，也没有原 /trace/tools 聚合入口。前端仍读 latencyMs，但新适配器未赋值 | O api/main.py:473、487、agents/agent_orchestrator.py:825；R resolvedesk/app.py:330、frontend/src/lib/resolvedesk.js:67 | G10 |

### 界面、配置与部署

| ID | 原技术行为 | 当前状态 | 实际差异或保留能力 | 关键证据 | 对齐包 |
| --- | --- | --- | --- | --- | --- |
| F01 | Vue 聊天、知识、监控、Skills、评测页面 | 替代 | 原 Vue 已接当前 API，并新增业务服务台、任务恢复等；相关数据仍受上方接口/统计缺口影响 | O 配套 EchoMindFrontend；R frontend/src/App.vue | — |
| F02 | Java/Python 双后端切换 | 范围选择 | 当前只开放 Python；Java 适配旧代码存在不代表 Java 服务已迁移 | O EchoMindFrontend/README.md；R frontend/src/App.vue、lib/resolvedesk.js | 明确不计 Python 欠账 |
| F03 | 交互式 CLI 使用同一记忆、角色、RAG、MCP | 未接入 | 原 `_cli` 留在旧 api/main.py；新主入口没有功能等价 CLI，smoke 脚本不是 CLI | O api/main.py:766；R tools/ | G11 |
| F04 | 部署配置与实际启动路径一致 | 部分 | 新入口固定本地路径/部分参数；.env.example 的 ALERT_WEBHOOK_URL、MCP_EXTERNAL_ENABLED、部分旧存储配置不驱动当前对应链路。新告警实际读取 RESOLVEDESK_ALERT_WEBHOOK | O .env.example、api/main.py:170；R .env.example、resolvedesk/app.py:85、monitoring.py:80 | G13 |
| F05 | Docker/Compose、Nginx网关、Prometheus抓取及数据挂载 | 保留未验 | 当前 Dockerfile 和 compose.yaml 仅单 app + data 挂载，未等价迁移原 Redis/Chroma/Prometheus/Nginx 服务编排；新机器镜像的前端构建、鉴权文件初始化及抓取需验收。Docker 按用户要求跳过 | O docker-compose.yml、docker-compose.web.yml、config/；R Dockerfile、compose.yaml、.dockerignore | G13 |

## 3. 固定的 13 类对齐包与完成标准

这些是本次已识别差异的归并，不是13个“还要再发明的新模块”。G12 的服务化存储/多进程及 G13 的 Docker 项可按用户当前范围明确延期，但不能称已迁移完成。

| 对齐包 | 一次性包含的工作 | 完成标准 |
| --- | --- | --- |
| G01 意图链路 | IT 域与细粒度意图映射、三路融合驱动实际路由、真实置信度/来源分数、低置信澄清、紧急升级；远端向量选项单列验收 | 同一原始请求从识别到实际路由的断言一致；覆盖融合冲突、未知意图、低置信及 CRITICAL；规则分类和三路识别分开报告 |
| G02 多轮状态 | IT实体类型、字段来源、跨轮继承、纠错和换任务清理，与工单草稿分开管理 | 多轮省略字段能继承；更换资产/资源/任务后旧字段不污染新任务；能追溯每字段来源 |
| G03 编排与故障处理 | 普通/复合请求统一故障语义、实例选优/探测、专业失败回退、升级交接、合成保留 partial/failed/实际角色 | 对普通和复合请求分别注入失败，核对实际执行角色、状态、回退轨迹与受控写入边界 |
| G04 专业节点工具 | 给各角色接权限范围内的共享 RAG/MCP；将旧电商内置工具适配 IT 职责 | 两个专业节点分别自主检索或调用允许的工具；不能越角色/用户权限；软件角色不再使用订单核验字段处理许可证 |
| G05 完整记忆维护 | 丰富画像、后台有界队列/合并互斥、分段情景、摘要合并及失败隔离 | 长对话和跨任务召回覆盖原能力；维护失败不使原本可完成的聊天失败；数据按租户/用户隔离 |
| G06 工具可靠性 | 原生检索纳入统一缓存/失效、超时、熔断与恢复、运行时知识故障回退；明确本地工具适用策略 | 真实当前入口验证缓存命中/重导入失效、超时、打开/半开/恢复及降级；不只测保留的类 |
| G07 MCP全链路 | 配置化外部 HTTP/stdio、身份头、专业节点接线、对外知识 MCP 服务端、受控调试；泛化写工具明确保留限制或另设确认契约 | 经正常启动配置完成外部只读服务的发现/调用/刷新/重连；其他标准客户端能按权限读取当前知识库，不能误把状态接口当协议端点 |
| G08 评测对齐 | 真正测当前三路识别器、逐轮 Judge、自定义用例/参考资料、通用意图/工具/实体断言、更多租户的合法数据入口 | 用同一套定义执行完整任务；早期坏回复可被发现；识别器错误不能被规则分类的28/28掩盖；失败和评分分母真实 |
| G09 监控对齐 | 主请求/专业执行/实际回退角色分账、单节点真实耗时、partial识别、Z-score、Histogram、工具熔断状态、延迟与失败反馈 | 注入已知延迟/失败后，指标数量、角色归因、直方图、告警和实际降级全部能对上；固定阈值差异明确记录 |
| G10 完整轨迹 | 本地与专业节点/MCP统一轨迹、脱敏参数/结果摘要、单工具耗时/错误/缓存/回退、分页/最近轨迹、前端耗时 | 从真实请求可还原完整调用链；原始敏感值不落轨迹；界面指标不出现 undefined 或固定伪置信度 |
| G11 API与运维入口 | 原接口映射/明确破坏性变更说明、服务端批量/文件导入、集成状态/调试、当前存储对应的交互CLI | 17个原HTTP接口及 MCP/CLI 每项都有可验证替代或明确退役；导入与权限/限制一致，调试不绕过写入审批 |
| G12 请求回执及存储并发 | 续聊逐轮幂等回执必须补齐；Redis/Chroma及多worker租约作为明确架构范围决策 | 对同一续聊请求重试返回同一结果且不重复调用模型/业务操作；若选择多worker，再验收跨进程互斥/失租保护，单进程版本不冒称分布式 |
| G13 配置与部署 | 清理或适配不生效的配置名、统一文档；外部Webhook接入；Docker镜像/Compose/网关/抓取/初始化按范围补验 | 新环境按文档一次部署可用；配置修改有明确效果；未做Docker和真实外部通知时持续标为延期/未接入 |

建议执行顺序：G01/G02/G03/G04 → G05/G06/G07 → G08/G09/G10/G11 → G12续聊回执与G13配置。服务化多worker、Docker和真实外部通知依既有约束另行验收，不因无法使用Docker而阻塞前面本机行为对齐。

## 4. 原版17个HTTP接口的完整去向

| 原接口 | 当前对应 | 状态/差异 |
| --- | --- | --- |
| GET /health | GET /health、/system | 健康能力保留，返回结构改变；不是模型可用性证明 |
| GET /skills | GET /skills | 已接入 |
| POST /skills/reload | POST /skills/reload | 已接入，support鉴权 |
| POST /chat | /runs、/reply、/events、/confirm 等 | 异步任务接口替代；旧响应元数据和逐轮回执并非完整等价 |
| GET /monitor | GET /monitor | 新监控替代，G09子项有差异 |
| GET /trace/tool/{request_id} | GET /trace/tool/{run_id} | 简化轨迹，G10未完成 |
| GET /trace/tools | 无等价聚合端点 | G10/G11 |
| GET /metrics | GET /metrics | 鉴权文本指标替代，原Histogram等未齐 |
| POST /search | POST /knowledge/search | 功能保留，query参数改为JSON |
| POST /knowledge/add | POST /knowledge/documents | 单篇替代；原服务端批量契约未齐 |
| POST /knowledge/upload | 浏览器解析后逐篇 /knowledge/documents | 没有服务端multipart等价端点，大小/批处理语义改变 |
| GET /knowledge/stats | GET /knowledge/stats | 已接入并限定可见范围 |
| POST /eval/run | POST /eval/run | 同路径但请求契约收窄；见G08 |
| GET /eval/jobs/{job_id} | GET /eval/jobs/{job_id} | 已接入，新增持久化、列表和显式重跑 |
| GET /integrations/embedding | /system 仅含检索向量状态 | 不等价于原意图embedding连接状态 |
| GET /integrations/mcp | GET /mcp | 连接状态查询替代 |
| POST /integrations/mcp/call | 无 | G07/G11 |

另有原 `app.mount('/mcp', ...)` 标准协议入口和 `--cli` 命令，均未在当前新主入口等价接入；它们不属于上述装饰器接口计数。

## 5. 全部21个原运行模块的覆盖记录

| 原文件 | 当前去向 |
| --- | --- |
| agents/agent_orchestrator.py | 专业节点部分调用；原完整 run/route/composer 不作为当前主循环 |
| agents/tools.py | 复合专业节点内置工具保留；共享工具需接线、领域需适配 |
| api/main.py | 旧兼容入口，不能用于证明新主入口完成 |
| core/access.py | 旧鉴权实现；新令牌/租户鉴权替代 |
| core/concurrency.py | 原租约/后台维护队列未进新主链路 |
| core/conversation_state.py | 原结构化实体状态合并未进新主链路 |
| core/intent_embedding.py | recognizer内部使用；远端默认未启用/验收 |
| core/intent_recognizer.py | 仍执行，但业务意图和路由由IT规则决定 |
| core/llm_utils.py | 原专业节点/Judge等组件复用 |
| core/redaction.py | 旧专业节点/轨迹组件保留；新完整轨迹待对齐 |
| core/skill_loader.py | 新主入口实际使用 |
| evaluation/evaluator.py | LLMJudge实际复用；原IntentEvaluator/EndToEndEvaluator不作为当前评测主循环 |
| evaluation/few_shot.py | Judge校准例实际复用 |
| evaluation/jobs.py | 新持久化EvaluationService替代 |
| memory/conversation_memory.py | 旧Redis/Chroma记忆实现保留；新ScopedMemory部分替代 |
| monitor/performance_monitor.py | 旧监控保留；新Monitor部分替代 |
| tooling/__init__.py | 包标记，无独立业务能力 |
| tooling/knowledge_base.py | 旧Chroma知识库保留；新HybridKnowledge替代 |
| tooling/mcp_client.py | 标准客户端引擎实际复用 |
| tooling/mcp_server.py | 仅旧入口配置/挂载，未服务于新知识库 |
| tooling/tool_manager.py | 当前MCP调用复用；原生RAG未完整接入其缓存/熔断/回退 |

## 6. 不应列为“还没迁移”的事项

- 当前确实新增并验收了持久检查点、SSE进度、精确草稿确认、权限过滤/版本引用、费用账本、模拟权限与维修事务、持久评测/告警、CI。这些成果不因本次发现差异而消失。
- 原版没有实际订单/退款/人工队列/企业SSO/IAM/CMDB/厂商授权集成，不能把接真实企业系统算成“迁移原能力”；这是未来产品化工作。
- 原场景扩展指南是12类建议，不是12个已实现系统；不要求为了迁移逐个实现。
- “500条”及对应大样本指标没有随包数据与可复现结果支撑，不纳入迁移要求；197个代码测试也不等于197条真实业务评测。
- 当前软件授权的席位目录仍为静态，网络场景主要是知识排障；它们需要进一步深化，但不能伪装成原版已有而漏迁的业务系统。
- 专用重排模型比较按用户要求不做；只用Flash属于明确选择。在线意图向量BGE-M3和知识检索E5是不同用途，不能以“不比较重排模型”作为不审计意图向量链路的理由。
- Java 21/Spring Boot/LangChain4j 是原包的独立后端；当前没有迁移或验收。其源码另有 AgentAiService、ManagedChatModel、AnswerVerifier、TextChunker、ToolRegistry 等实现。原Java已提供混合检索、输入预算和回答校验等机制，因此不能声称整个原包没有这些技术，更不能把Python范围的对比结果概括为对整个原包“碾压”。
- Java/Python双后端切换因选用Python未保留，作为范围选择记录，不再在以后突然追加成Python缺项。

## 7. 本轮交付与后续口径

本轮仅审计并保存证据，不修复业务代码、不切换存储、不启动Docker、不调用模型。原有“本地IT服务闭环验收通过”仍成立；“全部原技术完整迁移”“技术全面超过原包”目前不成立。

后续每次开发更新 G01–G13 的状态和证据；没有验收证据的项不能从“部分/未接入”改成“已完成”。验收矩阵固定后，新增产品需求、性能优化和新发现的独立缺陷分别记录，避免继续用一个模糊的“还有一点”代替完整范围说明。
