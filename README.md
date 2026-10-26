# ResolveDesk

面向内部 IT 支持的 Python Agent 项目：知识检索、专业角色处理、可恢复对话与用户确认后的工单创建。

当前主入口是 `resolvedesk/app.py`，Vue 界面位于 `frontend/`。项目可在本机独立运行，不依赖 Docker。

当前已实现范围与尚未迁移的旧功能见 [功能核对表](docs/FEATURE_STATUS.md)。本项目不能表述为原项目全部功能的等价超集。

## 本机启动

已配置环境时，在本目录运行：

```powershell
.\tools\start_local.ps1
```

打开 http://127.0.0.1:8080 。在页面“连接配置”中将地址设为 `/`，填入 `.codex-runtime/access.json` 中的本地访问令牌。`learner` 为普通用户，`support` 可导入知识、热加载 Skills 和运行评测。模型 API Key 不填到页面里。

新机器推荐 Python 3.12、Node.js 22.12 以上：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -X utf8 -B tools\setup_local.py --download-model
cd frontend
npm ci --no-audit --no-fund
npm run build
cd ..
.\tools\start_local.ps1
```

首次使用需将自己的 DeepSeek Key 配置到 `.env`，确认 `RESOLVEDESK_OWN_KEY_CONFIRMED=true`，按需启用 `RESOLVEDESK_PAID_API_ENABLED=true`。客户端只允许 `deepseek-flash`。

## 实际请求链路

Vue 工作台 → 带身份的任务 API → 持久化检查点 → 查询改写、混合检索、Flash 重排 → 记忆与意图路由 → Skills 与受限工具循环 → 回答/追问/草稿 → 用户确认 → SQLite 事务建单。

- **检索**：Markdown 标题与原文跨度分块，中文 BM25、多语言 E5 本地 ONNX 向量、RRF；查询改写及重排使用 Flash，失败记录原因并回退。
- **路由与协作**：复用 `agents/agent_orchestrator.py` 的角色定义、意图融合、路由和专业节点。常规请求按主角色处理；复合问题实际并行调用专业节点，再交给当前任务综合处理。
- **记忆**：SQLite 保存会话摘要、用户明确提及的系统/IDE 信息和语义历史；摘要按长度触发，跨任务检索限定当前用户与租户。历史不是知识库政策依据。
- **Skills**：加载 `skills/` 中当前版本的业务说明，按角色和关键词注入，忽略 history，客服可热加载。
- **MCP**：官方 SDK 的标准 stdio 客户端实际连接 `resolvedesk/mcp_support.py`。仅开放白名单只读诊断清单，不把模型密钥传给子进程。
- **工单**：只允许模型准备草稿；标题、描述、类别缺失时追问。确认绑定精确草稿版本且有有效期。确认与创建在同一事务内，重复确认返回同一工单。
- **权限**：令牌绑定租户/用户/角色；知识在召回前过滤；普通用户只读自己的任务、工单和记忆。
- **流式**：真实 SDK 文本增量经持久化 SSE 事件送到 Vue。生成中的文字标为待校验；最终回答经过来源检查。事件连接支持 Last-Event-ID 和当前轮次游标。
- **恢复**：每次模型结果及工具轮次保存检查点。重启后活动任务暂停；显式恢复可复用已保存模型结果和工具输出。未完成或未保存的模型调用仍可能重新发生并收费；写操作始终走独立确认事务。
- **评测**：评测任务与报告持久化，默认执行本地检索集；负反馈由客服指定期望文档/章节，经审核后加入并执行回归集。界面不把检索指标当成 LLM 回复质量分数。

## 测试与证据

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m pytest -q
cd frontend
npm test
npm run build
```

pytest 默认关闭付费调用，网关测试使用受控 SDK/HTTP。集成测试包含真实本机 MCP 子进程、权限隔离、摘要/语义记忆、断点恢复、流式取消记账、重排回退和反馈回归。

以下命令会实际调用 Flash，并保留明确的本地演示记录：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B tools\smoke_local.py
.\.venv\Scripts\python.exe -X utf8 -B tools\smoke_integrated.py
```

`smoke_integrated.py` 的长对话测试会明确标记并注入合成上下文样本，再通过真实服务验证摘要、记忆、专业节点和 MCP。它不是实际用户使用记录。报告保存在 `reports/`。

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
| core/paid_api.py | Flash 预算、usage、流式和失败预留 |
| agents、core、tooling | 角色、意图、Skill、标准 MCP 等复用组件 |
| memory/conversation_memory.py、api/main.py | Redis/Chroma 兼容链路；当前主入口不依赖这条存储链路 |

## API 和持久数据

业务接口要求 `Authorization: Bearer <本地令牌>`。

- `/runs`：创建/列出任务；`/runs/{id}` 查询；`/reply` 补充；`/confirm` 确认建单；`/pause`、`/resume`；`/events`；`/messages`。
- `/tickets`、`/tickets/{id}`：按权限查询工单。
- `/knowledge/search`、`/knowledge/documents`、`/knowledge/stats`、`/knowledge/citations/{id}`。
- `/skills`、`/skills/reload`、`/memory`、`/mcp`、`/monitor`、`/system`。
- `/eval/run`、`/eval/jobs/{id}`、`/eval/jobs/{id}/resume`。
- `/runs/{id}/feedback`、`/evaluation/candidates`、`/evaluation/candidates/{id}/promote`。

`data/resolvedesk.sqlite3` 保存业务、记忆和评测；`data/billing/usage.sqlite3` 保存累计费用；`data/models/` 保存本地模型。不要通过删除费用账本重置预算。

`.env`、`key.txt`、`.codex-runtime/`、`data/` 和 `history/` 不应提交或上传。前端构建不复制 public/history，后端只公开明确的页面和 assets，不公开源码或历史目录。

## 预算与边界

项目累计 API 预算为人民币 20 元，预留 2 元安全余量。所有接入网关的调用共享同一账本。按高峰缓存未命中单价保守预留并结算；超时、取消或缺少 usage 保留预留。该记账不是服务商账号侧消费硬上限，也不包含其他程序的消费。

没有比较专用重排模型。当前知识集和评测样本很小，不能据此宣称生产质量或普遍性能提升。引用存在不等于每句话均被原文支持。

当前服务只监听本机，单进程；MCP 仅白名单只读工具。工单存在本地，未连接企业 SSO 或外部工单平台。没有完成生产压测、集群容灾；Docker 构建与容器运行按要求跳过。

本项目按用户最新要求使用 Git 提交保存代码版本，不再生成相邻 history 文件夹。修改前先检查状态并提交必要的检查点。Git 不会自动保存未提交改动，也不保存被忽略的数据库、费用账本、模型和密钥；这些资料需独立备份。

GitHub Actions 在推送和 PR 时检查 Windows/Linux Python 回归、敏感文件排除、Vue 测试和构建。检查不会使用真实模型密钥或调用付费 API。门禁通过也不等于功能全部迁移，具体范围仍以核对表和实际运行证据为准。
