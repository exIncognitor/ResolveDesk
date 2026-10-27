ResolveDesk 使用入口

完整的当前说明见本目录 README.md，包含实际调用链、部署、验证、预算和功能边界。

启动：在本目录 PowerShell 执行 .\tools\start_local.ps1
页面：http://127.0.0.1:8080
接口：http://127.0.0.1:8080/docs

当前页面使用 frontend/ 下的 Vue 工作台，已经接入当前 Python 服务。
在连接配置中填写 / 和 .codex-runtime/access.json 里的本地令牌。
不要把 DeepSeek API Key 填到页面。

原有目录中的 role、Skill、意图、MCP 组件已经通过 resolvedesk/services.py 接入当前服务。
主服务使用 PostgreSQL 保存业务、摘要与评测，Redis 负责执行租约，Chroma 负责知识向量索引。
先运行 tools/setup_services.py，启动 compose.services.yaml 的依赖，再运行 Python 服务；完整部署使用 compose.yaml。
旧 data/resolvedesk.sqlite3 保留为迁移来源，不再由主入口写入。完整记忆、评测、监控和轨迹已有独立验收，最终逐包状态见 docs/MIGRATION_PROGRESS.md。

只使用 deepseek-flash；累计预算 20 元，保留 2 元余量。唯一运行费用源是 PostgreSQL 的 resolvedesk_finance.paid_calls；data/billing/usage.sqlite3 只保留为切换前旧账本，不要用旧账本回滚消费。
测试结果与说明在 reports/。Docker 已可用；企业 SSO、外部工单平台和生产级容灾尚未验收。

按用户最新要求改用 Git 提交留存代码，不再生成相邻 history 文件夹。未提交改动及忽略的数据库、费用账本、模型和密钥不由 Git 自动备份。
交互CLI：.\.venv\Scripts\python.exe -X utf8 -B -m resolvedesk.cli --base-url http://127.0.0.1:18080 --actor learner
输入 :help 查看命令；令牌从本地 access.json 读取，网络重试用 :retry，草稿核对版本后用 :confirm VERSION。
固定Python迁移范围及明确未验收项见 docs/FEATURE_STATUS.md；真实外部告警按用户选择暂不配置，不能声称已经发送企业通知。
