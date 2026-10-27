# ResolveDesk

面向内部 IT 服务台的 Python Agent 项目：账号权限、网络终端、软件授权、设备服务，包含知识检索、多角色协作、可恢复对话、权限审批和设备维修模拟闭环。

当前主入口是 `resolvedesk/app.py`，Vue 界面位于 `frontend/`。主服务使用 PostgreSQL、Redis、Chroma；开发时可在宿主机运行 Python，依赖由 Docker 管理。SQLite 仅用于显式离线测试和保留旧数据。

当前已实现范围与尚未迁移的旧功能见 [功能核对表](docs/FEATURE_STATUS.md)。本项目不能表述为原项目全部功能的等价超集。

四类场景、状态机、隔离质量评测与告警配置见 [IT 服务台设计与验收](docs/IT_SERVICE_DESIGN.md)。业务数据和企业制度均明确标注为合成模拟。

## 本机启动

已配置环境时，在本目录运行：

```powershell
docker compose --env-file .codex-runtime/services.env -f compose.services.yaml up -d
.\tools\start_local.ps1
```

打开 http://127.0.0.1:8080 。在页面“连接配置”中将地址设为 `/`，填入 `.codex-runtime/access.json` 中的本地访问令牌。`learner` 为普通用户，`support` 可导入知识、热加载 Skills 和运行评测。模型 API Key 不填到页面里。

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
.\tools\start_local.ps1
```

首次使用需将自己的 DeepSeek Key 配置到 `.env`，确认 `RESOLVEDESK_OWN_KEY_CONFIRMED=true`，按需启用 `RESOLVEDESK_PAID_API_ENABLED=true`。客户端只允许 `deepseek-flash`。

已有旧 SQLite 数据时先停止旧服务，执行 `tools/migrate_services.py --apply`；工具先建立业务库和累计费用账本的一致性快照，只允许向空 PostgreSQL 目标迁移，逐表校验行数和内容摘要。不要先在目标运行演示数据初始化。当前切换证据见 [存储切换记录](docs/SERVICE_CUTOVER_2026-10-27.md)。

完整容器部署包含前端构建、应用、网关、PostgreSQL、Redis、Chroma、Prometheus：

```powershell
docker compose --env-file .codex-runtime/services.env -f compose.yaml up -d --build
```

网关默认 `http://127.0.0.1:18080`，Prometheus 默认 `http://127.0.0.1:19090`。模型、身份和原费用账本通过 `data/` 挂载保留；镜像不包含密钥。`/health` 报告技术路径，`/ready` 检查依赖。

## 实际请求链路

Vue 工作台 → 带身份与逐轮请求键的 API → Redis 租约及 SQL 认领代次 → IT三路意图融合与多轮字段状态 → 记忆/查询改写/混合检索 → 普通或复合只读专业节点 → 有界协调工具循环 → 回答/追问/草稿 → 用户确认 → PostgreSQL 事务建单。低置信直接追问；高风险交接直接呈现当前授权安全制度，不转成未经请求的工单收集。

- **检索**：Markdown 标题与原文跨度分块，中文 BM25、多语言 E5 本地 ONNX 向量、Chroma HNSW、RRF；Chroma 召回前过滤权限，再向 SQL 校验当前授权和版本。文档更新与索引 outbox 同事务，失败后投影最新状态。查询改写及重排使用 Flash，失败记录原因并回退。
- **路由与协作**：Flash语义、本地固定E5、IT规则三路融合驱动实际路由，记录来源、有效权重和融合分数（不冒称校准概率）。普通与复合问题均调用显式IT专业节点；真实失败会回退，保留实际角色及partial/failed。节点冷却后有界探测，分节点检查点避免恢复时重复已完成的只读调用。
- **实体与专业工具**：独立entity_state保留用户明确给出的资源/资产/权限/期限/环境及turn_seq来源，纠错优先于模型旧参数，换任务清除旧草稿。各专业节点自主使用按当前身份过滤的RAG、引用、业务目录、本人资产或许可核验工具，保留原确定性技术诊断；不再使用订单核验字段处理软件授权。多资产/资源未明确选择时禁止模型代选。
- **记忆**：PostgreSQL 保存会话摘要、用户明确提及的系统/IDE 信息和语义历史；完整 Redis 工作记忆、分段 Chroma 情景记忆仍在 G05 实施范围内。历史不是知识库政策依据。
- **Skills**：加载 `skills/` 中当前版本的业务说明，按角色和关键词注入，忽略 history，客服可热加载。
- **MCP**：官方 SDK 的标准 stdio 客户端实际连接 `resolvedesk/mcp_support.py`。仅开放白名单只读诊断清单，不把模型密钥传给子进程。
- **向量选项**：意图默认复用本地E5；`RESOLVEDESK_INTENT_VECTOR_PROVIDER=self_hosted_http`可连接独立鉴权的自托管同版本E5。实际HTTP向量一致性和配置到路由链路已测试，不会自动使用旧第三方Key。配置及验证见 `docs/IT_ENGINE_MIGRATION_2026-10-27.md`。
- **工单**：只允许模型准备草稿；标题、描述、类别缺失时追问。确认绑定精确草稿版本且有有效期。确认与创建在同一事务内，重复确认返回同一工单。
- **业务闭环**：权限申请确认后待审批，其他 IT 操作员批准后再模拟开通，实际写入有期限的本地权限表。设备维修校验本人资产，经过分诊、派单、处理、本人验收或返工。每步有版本检查、幂等与事务审计。
- **权限**：令牌绑定租户/用户/角色；知识在召回前过滤；普通用户只读自己的任务、工单和记忆。
- **流式**：真实 SDK 文本增量经持久化 SSE 事件送到 Vue。生成中的文字标为待校验；最终回答经过来源检查。事件连接支持 Last-Event-ID 和当前轮次游标。
- **恢复与并发**：每次模型结果及工具轮次保存检查点。Redis 持续续租、SQL owner/epoch/有效期阻止旧执行者提交；启动仅暂停过期执行，不中断其他工作进程。每轮回执持久化，同键不同输入拒绝；重试读取原轮次答案及事件范围。显式恢复可复用已保存结果，未完成的模型调用仍可能重发并收费。
- **评测**：免费检索回归与完整付费质量评测分开。质量评测经隔离的当前 HTTP 入口执行真实对话和业务事务，检查路由、引用、工具、申请字段、业务结果，用四维 Flash Judge 评分；失败独立报告，人工选择同套件基线做回归。支持任务记录和显式重新执行。
- **监控**：真实角色、工具、主执行和检索指标持久化，提供鉴权 Prometheus 指标、告警触发/恢复、持久化通知 outbox 和路由降权。Webhook 地址未配置时仅本地留存事件。

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

`accept_it_service.py --quality` 的旧28条意图指标仅测规则，完整替换为当前融合识别器评测仍归G08；不要把28/28当作新识别器准确率。G01–G04已有隔离真实依赖与Flash的8场景验证，以及两个专业节点自主RAG/MCP验证，见 `docs/IT_ENGINE_MIGRATION_2026-10-27.md`。固定检索集20条，另加审核回归。`--live-workflows`会在主库新增模拟业务记录。不存在随包的500条生产测试数据，小样本不能证明泛化效果。

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
- `/knowledge/search`、`/knowledge/documents`、`/knowledge/stats`、`/knowledge/citations/{id}`。
- `/skills`、`/skills/reload`、`/memory`、`/mcp`、`/monitor`、`/system`。
- `/metrics`：support 鉴权的 Prometheus 指标。
- `/eval/run`、`/eval/jobs`、`/eval/jobs/{id}`、`/eval/jobs/{id}/resume`、`/eval/jobs/{id}/baseline`。
- `/runs/{id}/feedback`、`/evaluation/candidates`、`/evaluation/candidates/{id}/promote`。

PostgreSQL 保存业务、记忆和评测；`data/resolvedesk.sqlite3` 保留为旧库，主服务不再写入；`data/billing/usage.sqlite3` 继续保存原累计费用；`data/models/` 保存本地模型。不要通过删除费用账本重置预算。

`.env`、`key.txt`、`.codex-runtime/`、`data/` 和 `history/` 不应提交或上传。前端构建不复制 public/history，后端只公开明确的页面和 assets，不公开源码或历史目录。

## 预算与边界

项目累计 API 预算为人民币 20 元，预留 2 元安全余量。所有接入网关的调用共享同一账本。按高峰缓存未命中单价保守预留并结算；超时、取消或缺少 usage 保留预留。该记账不是服务商账号侧消费硬上限，也不包含其他程序的消费。

没有比较专用重排模型。当前知识集和评测样本很小，不能据此宣称生产质量或普遍性能提升。引用存在不等于每句话均被原文支持。

当前端口只监听本机；MCP 仅白名单只读工具。多工作进程共享 PostgreSQL 和 Redis，业务与租约已做竞争测试。企业 SSO、外部工单平台、生产压测和集群容灾尚未验收；完整 G01–G13 进度见 `docs/MIGRATION_PROGRESS.md`。

本项目按用户最新要求使用 Git 提交保存代码版本，不再生成相邻 history 文件夹。修改前先检查状态并提交必要的检查点。Git 不会自动保存未提交改动，也不保存被忽略的数据库、费用账本、模型和密钥；这些资料需独立备份。

GitHub Actions 在推送和 PR 时检查 Windows/Linux Python 回归、敏感文件排除、Vue 测试和构建。检查不会使用真实模型密钥或调用付费 API。门禁通过也不等于功能全部迁移，具体范围仍以核对表和实际运行证据为准。
