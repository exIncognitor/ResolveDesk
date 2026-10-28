# ResolveDesk

面向内部 IT 服务台的 Python Agent 项目：账号权限、网络终端、软件授权、设备服务，包含知识检索、多角色协作、可恢复对话，以及权限审批、设备维修、软件席位三条模拟闭环。

当前主入口是 `resolvedesk/app.py`，Vue 界面位于 `frontend/`。主服务使用 PostgreSQL、Redis、Chroma；开发时可在宿主机运行 Python，依赖由 Docker 管理。SQLite 仅用于显式离线测试和保留旧数据。

当前实现范围和验证边界见 [功能核对表](docs/FEATURE_STATUS.md)及[技术范围](docs/TECHNICAL_SCOPE.md)，来源与授权记录见 [SOURCES.md](SOURCES.md)。

当前执行链、技术取舍与文件/函数入口见 [当前架构](docs/CURRENT_ARCHITECTURE.md)，从零演示见 [三条业务闭环](docs/BUSINESS_DEMOS.md)。早期 [IT 服务台设计与验收](docs/IT_SERVICE_DESIGN.md) 已标为历史阶段。业务数据和企业制度均明确标注为合成模拟。

第四轮修复执行契约、全入口知识草稿治理和发布评测身份，最终 Flash 两轮硬指标均为 12/12，辅助 Judge 均为 9/12；完整证据、保留的质量缺口与恢复方式见 [第四轮验收](docs/ITERATION4_ACCEPTANCE.md)。

当前展示版本的身份/会话切换修复、三条浏览器业务闭环、42 项前端与 463 项服务回归、模型复验及恢复位置见 [收尾验收](docs/FINAL_POLISH_ACCEPTANCE.md)。详细教材另存于本机相邻目录 `agentlearn/ResolveDesk/`。

后端只使用Python；原Java版本是另一套学习实现，不作为迁移欠项。第二轮固定目标见[目标提示词](docs/NEXT_ITERATION_GOAL.md)，实测性能、冻结留出集、软件席位与失败候选记录见[第二轮交付](docs/ITERATION2_ACCEPTANCE.md)。普通检索默认复用BM25索引和文档版本水位；节省模型调用的adaptive策略因留出集退化保持关闭，默认仍为完整专家编排。

## 本机启动

已配置环境时，在本目录运行：

```powershell
docker compose --env-file .codex-runtime/services.env -f compose.services.yaml up -d
.\tools\start_local.ps1
```

打开 http://127.0.0.1:8080 。在页面“连接配置”中将地址设为 `/`，填入 `.codex-runtime/access.json` 中的本地访问令牌。`learner` 为普通用户，`support` 可导入知识、热加载 Skills 和运行评测。模型 API Key 不填到页面里。

当前本机升级运行时为`.codex-runtime/python`，启动脚本优先使用它，原`.venv`保留作基线。可用`-PythonExecutable`显式指定解释器；新机器按下方步骤创建的`.venv`仍是启动脚本的回退选项。

新机器推荐 Python 3.12、Node.js 22.12 以上：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -X utf8 -B tools\setup_services.py
docker compose --env-file .codex-runtime/services.env -f compose.services.yaml up -d
.\.venv\Scripts\python.exe -X utf8 -B tools\setup_local.py --download-model
.\.venv\Scripts\python.exe -X utf8 -B tools\setup_services.py
cd frontend
npm ci --no-audit --no-fund
npm run build
cd ..
.\.venv\Scripts\python.exe -X utf8 -B tools\migrate_financial_ledger.py --initialize-new
.\tools\start_local.ps1
```

首次使用需将自己的 DeepSeek Key 配置到 `.env`，确认 `RESOLVEDESK_OWN_KEY_CONFIRMED=true`，按需启用 `RESOLVEDESK_PAID_API_ENABLED=true`。客户端只允许 `deepseek-flash`。

`setup_local.py` 导入的内置知识首次为草稿。打开知识页，使用 support 补齐材料类型（模拟制度）、适用范围、来源核对日期和有效期，保存并审核发布后才可检索。来源核对和存量纳管说明见 [知识发布操作](docs/KNOWLEDGE_PUBLICATION.md)。

已有旧 SQLite 数据时先停止旧服务，执行 `tools/migrate_services.py --apply`；工具先建立业务库和累计费用账本的一致性快照，只允许向空 PostgreSQL 目标迁移，逐表校验行数和内容摘要。不要先在目标运行演示数据初始化。当前切换证据见 [存储切换记录](docs/SERVICE_CUTOVER_2026-10-27.md)。

已有费用账本首次切换时，停止宿主机应用和容器应用，执行 `tools/migrate_financial_ledger.py --apply`。只导入空的 PostgreSQL 费用目标，完整保留既有调用和未知预留；费用已经迁移后无需重跑。上面的 `--initialize-new` 仅用于没有旧费用账本的全新安装，拒绝覆盖既有账本。运行时不会自行创建零消费账本。验证和恢复位置见 [记忆与费用切换记录](docs/MEMORY_MIGRATION_2026-10-27.md)。

完整容器部署包含前端构建、应用、网关、PostgreSQL、Redis、Chroma、Prometheus：

```powershell
docker compose --env-file .codex-runtime/services.env -f compose.yaml up -d --build
```

网关默认 `http://127.0.0.1:18080`，Prometheus 默认 `http://127.0.0.1:19090`。模型、身份和原费用账本通过 `data/` 挂载保留；镜像不包含密钥。`/health` 报告技术路径，`/ready` 检查依赖。

当前服务也有交互CLI，和网页共用身份、会话、记忆、知识、专业节点及MCP：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m resolvedesk.cli --base-url http://127.0.0.1:18080 --actor learner
```

输入`:help`查看命令。CLI读取本地`.codex-runtime/access.json`中的API令牌，不需要把模型Key放进终端。网络结果不确定时用`:retry`复用原请求；草稿以`:confirm VERSION`核对后提交模拟工单。接口映射、批量上传与CLI契约见 [API与CLI迁移](docs/API_CLI_MIGRATION_2026-10-27.md)。

## 实际请求链路

Vue 工作台 → 带身份与逐轮请求键的 API → Redis 租约及 SQL 认领代次 → IT三路意图融合与多轮字段状态 → 记忆/查询改写/混合检索 → 普通或复合只读专业节点 → 有界协调工具循环 → 回答/追问/草稿 → 用户确认 → PostgreSQL 事务建单。低置信直接追问；高风险交接直接呈现当前授权安全制度，不转成未经请求的工单收集。

- **检索**：Markdown标题与原文跨度分块、中文BM25、本地固定E5、Chroma HNSW和RRF。主聊天/专业节点/知识搜索API共用工具管理器；完整缓存先于付费改写/重排，Redis可跨进程复用。缓存键包含身份、执行角色、文档权限/版本与索引水位，命中仍经SQL复核。原生检索有独立超时、有界线程、熔断与恢复探测；hybrid故障明确回退BM25，显式vector故障返回不可用。文档更新与索引outbox同事务；生成批次和最终引用重新核验。详见`docs/RETRIEVAL_RELIABILITY_2026-10-27.md`。
- **路由与协作**：Flash语义、本地固定E5、IT规则三路融合驱动实际路由，记录来源、有效权重和融合分数（不冒称校准概率）。普通与复合问题均调用显式IT专业节点；真实失败会回退，保留实际角色及partial/failed。节点冷却后有界探测，分节点检查点避免恢复时重复已完成的只读调用。
- **实体与专业工具**：独立entity_state保留用户明确给出的资源/资产/权限/期限/环境及turn_seq来源，纠错优先于模型旧参数，换任务清除旧草稿。各专业节点自主使用按当前身份过滤的RAG、引用、业务目录、本人资产或许可核验工具，保留原确定性技术诊断；不再使用订单核验字段处理软件授权。多资产/资源未明确选择时禁止模型代选。
- **记忆**：PostgreSQL 保存可追溯摘要、画像原文依据、分段当前指针与有界维护队列；Redis 缓存工作上下文，独立私有 Chroma 集合同会话优先、跨会话补充召回。模型压缩和画像在固定后台池中维护，前台不等待模型；故障保留原文并显式降级，按租户与本人隔离。历史不是知识库政策依据。
- **Skills**：加载 `skills/` 中当前版本的业务说明，按角色和关键词注入，忽略 history，客服可热加载。
- **MCP**：正常启动从JSON加载多服务stdio/Streamable HTTP与专用身份头，限定只读工具及专业角色；主动ping支持空闲掉线重连，目录通知刷新后重新注册。`GET /mcp`为状态，`/mcp/knowledge/`为现有Bearer令牌鉴权的标准知识协议，支持Tools/计数/引用Resource。support专用调试不调用模型；传输凭据脱敏。配置和边界见`docs/MCP_MIGRATION_2026-10-27.md`。
- **向量选项**：意图默认复用本地E5；`RESOLVEDESK_INTENT_VECTOR_PROVIDER=self_hosted_http`可连接独立鉴权的自托管同版本E5。实际HTTP向量一致性和配置到路由链路已测试，不会自动使用旧第三方Key。配置及验证见 `docs/IT_ENGINE_MIGRATION_2026-10-27.md`。
- **工单**：只允许模型准备草稿；标题、描述、类别缺失时追问。确认绑定精确草稿版本且有有效期。确认与创建在同一事务内，重复确认返回同一工单。
- **业务闭环**：权限申请确认后待审批，其他 IT 操作员批准后再模拟开通，实际写入有期限的本地权限表。设备维修校验本人资产，经过分诊、派单、处理、本人验收或返工。软件授权确认后由其他操作员审批、预占、分配；15分钟预占或授权到期回收，本人或同租户support可回收已分配席位。产品锁防超分配，禁止自批，版本/幂等/事务审计和Vue入口共用服务端规则。均不接真实厂商系统。
- **权限**：令牌绑定租户/用户/角色；知识在召回前过滤；普通用户只读自己的任务、工单和记忆。
- **流式**：真实 SDK 文本增量经持久化 SSE 事件送到 Vue。生成中的文字标为待校验；最终回答经过来源检查。事件连接支持 Last-Event-ID 和当前轮次游标。
- **恢复与并发**：每次模型结果及工具轮次保存检查点。Redis 持续续租、SQL owner/epoch/有效期阻止旧执行者提交；启动仅暂停过期执行，不中断其他工作进程。每轮回执持久化，同键不同输入拒绝；重试读取原轮次答案及事件范围。显式恢复可复用已保存结果，未完成的模型调用仍可能重发并收费。
- **评测**：免费检索回归与付费质量评测分开；测当前三路IT识别器，经隔离的PG/Redis/Chroma和当前HTTP入口执行多轮对话及业务事务，逐轮四维Judge与通用断言。自定义数据/最多50轮、持久总时限、未知付费步骤不重调、跨版本有效基线和受保护的保留/回收均已验。开发意图集51/52，保留401场景误判；详见`docs/EVALUATION_MIGRATION_2026-10-27.md`。
- **监控**：主执行epoch、实际专家角色/实例、工具与检索分开统计，持久Counter/Histogram在原始遥测清理后保留；Z-score、固定阈值、依赖熔断、结构化建议及失败/延迟共同降权、唯一恢复探测。观察超时不替换工具结果，未知中断耗时保持null。详见`docs/MONITORING_MIGRATION_2026-10-27.md`。Webhook未配置，仅本地outbox；用户选择暂不验收真实外部通知。
- **轨迹与导入**：主/预检索/专业/MCP调用统一脱敏参数、结果摘要、缓存/回退、错误和实际耗时，按原轮次稳定分页及最近列表。知识支持整批SQL原子导入及服务端10 MiB multipart，最多100篇；Chroma由同事务outbox投影，不把跨存储投影称为SQL原子提交。

## 测试与证据

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m pytest -q
cd frontend
npm test
npm run build
```

pytest 默认关闭付费调用，网关测试使用受控 SDK/HTTP。集成测试包含真实本机 MCP 子进程、权限隔离、摘要/语义记忆、断点恢复、流式取消记账、重排回退和反馈回归。

设置 `RESOLVEDESK_SERVICE_TESTS=true` 会额外运行真实 Redis/PostgreSQL/Chroma 测试，使用独立随机测试 schema、键空间和集合。免费实时检索验收：`python -X utf8 -B tools/verify_service_runtime.py`。

以下命令会实际调用 Flash，并保留明确的本地演示记录：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B tools\smoke_local.py
.\.venv\Scripts\python.exe -X utf8 -B tools\smoke_integrated.py
.\.venv\Scripts\python.exe -X utf8 -B tools\accept_it_service.py --quality
```

`smoke_integrated.py` 的长对话测试会明确标记并注入合成上下文样本，再通过真实服务验证摘要、记忆、专业节点和 MCP。它不是实际用户使用记录。报告保存在 `reports/`。

`accept_it_service.py --quality`保留旧规则评测，28/28不能当作当前三路识别器准确率。当前融合识别、逐轮完整对话及业务评测由`/eval/run`执行，G08真实开发集为51/52，含一例401归属误判，见`docs/EVALUATION_MIGRATION_2026-10-27.md`。固定检索集20条，另加审核回归。`--live-workflows`会在主库新增模拟业务记录。不存在随包的500条生产测试数据，小样本不能证明泛化效果。

## 关键目录

| 路径 | 作用 |
| --- | --- |
| frontend/src/App.vue | Vue 工作台与工单、任务、知识、评测页面 |
| frontend/src/lib/resolvedesk.js | 当前 API 适配与增量 SSE 解析 |
| resolvedesk/app.py | 当前 API、身份、前端静态资源和服务生命周期 |
| resolvedesk/agent.py | 有上限的工具循环、回答校验、流式与检查点 |
| resolvedesk/services.py | 实际接入的路由、记忆、Skills、优化检索与 MCP |
| resolvedesk/store.py | 任务、检查点、确认与工单事务 |
| resolvedesk/evaluations.py | 持久评测与人工审核的反馈回归 |
| resolvedesk/quality.py | 经当前 HTTP 入口的隔离质量评测与实际业务结果核验 |
| resolvedesk/business.py | 模拟资源、权限、资产、维修状态与审计 |
| resolvedesk/monitoring.py | 运行指标、告警生命周期与 Webhook outbox |
| core/paid_api.py | Flash 预算、usage、流式和失败预留 |
| agents、core、tooling | 角色、意图、Skill、标准 MCP 等复用组件 |
| resolvedesk/database.py、coordination.py、vectors.py | PostgreSQL 连接、Redis 租约与 Chroma 有界 HTTP 客户端 |
| memory/conversation_memory.py、api/main.py | 保留的兼容入口，不等于全部能力已经接入新主入口 |

## API 和持久数据

业务接口要求 `Authorization: Bearer <本地令牌>`。

- `/runs`：创建/列出任务；`/runs/{id}` 查询；`/reply` 补充（必须有 request_key）；`/turns/{request_key}` 读取该轮回执；`/confirm` 确认建单；`/pause`、`/resume`；`/events?turn_key=...`；`/messages`。
- `/runs/{id}/state`：当前请求所属用户的多轮字段、来源与任务代次；`/system`的support视图报告实际识别器、向量提供方、专业类及工具白名单。
- `/tickets`、`/tickets/{id}`：按权限查询工单。
- `/service-catalog`、`/service-cases`、`/service-cases/{id}`、`/service-cases/{id}/actions`：模拟业务目录、状态、鉴权操作与审计。
- `/knowledge/search`、`/knowledge/documents`、`/knowledge/add`（或`/batch`）、`/knowledge/upload`、`/knowledge/stats`、`/knowledge/citations/{id}`；导入需support，上传UTF-8 .txt/.md/.json，文件10 MiB、JSON最多100篇。
- `/skills`、`/skills/reload`、`/memory`、`/mcp`、`/monitor`、`/system`。
- `/integrations/mcp`、`/integrations/mcp/call`（support只读调试）、`/integrations/embedding`（意图向量状态，与`/intent-embedding`同处理器）；标准协议为`/mcp/knowledge/`。
- `/trace/tool/{id}?turn_key=...&after=...&through=...`：next_after分页，固定首个snapshot_end；`/trace/tools?before=...`：本人最近轮次。
- `/metrics`：support 鉴权的 Prometheus 指标。
- `/eval/run`、`/eval/jobs`、`/eval/jobs/{id}`、`/eval/jobs/{id}/resume`、`/eval/jobs/{id}/baseline`。
- `/runs/{id}/feedback`、`/evaluation/candidates`、`/evaluation/candidates/{id}/promote`。

PostgreSQL 保存业务、记忆和评测，`resolvedesk_finance.paid_calls` 保存全进程共享的累计费用；`data/resolvedesk.sqlite3` 与 `data/billing/usage.sqlite3` 保留为切换前旧库，主服务不再写入它们；`data/models/` 保存本地模型。不要通过删除或恢复旧费用账本重置预算。原 `RESOLVEDESK_LEDGER_PATH` 配置已经退役，不会改变正式费用来源。

`.env`、`key.txt`、`.codex-runtime/`、`data/` 和 `history/` 不应提交或上传。前端构建不复制 public/history，后端只公开明确的页面和 assets，不公开源码或历史目录。

## 预算与边界

项目累计 API 预算为人民币 20 元，预留 2 元安全余量。所有接入网关的调用共享同一账本。按高峰缓存未命中单价保守预留并结算；超时、取消或缺少 usage 保留预留。该记账不是服务商账号侧消费硬上限，也不包含其他程序的消费。

没有比较专用重排模型。当前知识集和评测样本很小，不能据此宣称生产质量或普遍性能提升。引用存在不等于每句话均被原文支持。

当前端口只监听本机；MCP 仅白名单只读工具。多工作进程共享 PostgreSQL 和 Redis，业务与租约已做竞争测试。企业 SSO、外部工单平台、生产压测和集群容灾尚未验收；完整 G01–G13 进度见 `docs/MIGRATION_PROGRESS.md`。

本项目按用户最新要求使用 Git 提交保存代码版本，不再生成相邻 history 文件夹。修改前先检查状态并提交必要的检查点。Git 不会自动保存未提交改动，也不保存被忽略的数据库、费用账本、模型和密钥；这些资料需独立备份。

GitHub Actions 在推送和 PR 时检查 Windows/Linux Python 回归、敏感文件排除、Vue 测试和构建。检查不会使用真实模型密钥或调用付费 API。门禁通过也不等于功能全部迁移，具体范围仍以核对表和实际运行证据为准。
