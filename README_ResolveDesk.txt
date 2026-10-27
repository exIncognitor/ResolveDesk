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
旧 data/resolvedesk.sqlite3 保留为迁移来源，不再由主入口写入。完整记忆和其余技术迁移仍按 docs/MIGRATION_PROGRESS.md 推进。

只使用 deepseek-flash；累计预算 20 元，保留 2 元余量。费用账本位于 data/billing/usage.sqlite3，不要删除。
测试结果与说明在 reports/。Docker 已可用；企业 SSO、外部工单平台和生产级容灾尚未验收。

按用户最新要求改用 Git 提交留存代码，不再生成相邻 history 文件夹。未提交改动及忽略的数据库、费用账本、模型和密钥不由 Git 自动备份。
目前不能称为原项目所有功能加全部改进，已接入与未迁移能力详见 docs/FEATURE_STATUS.md。
