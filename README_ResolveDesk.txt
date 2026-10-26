ResolveDesk 使用入口

完整的当前说明见本目录 README.md，包含实际调用链、部署、验证、预算和功能边界。

启动：在本目录 PowerShell 执行 .\tools\start_local.ps1
页面：http://127.0.0.1:8080
接口：http://127.0.0.1:8080/docs

当前页面使用 frontend/ 下的 Vue 工作台，已经接入当前 Python 服务。
在连接配置中填写 / 和 .codex-runtime/access.json 里的本地令牌。
不要把 DeepSeek API Key 填到页面。

原有目录中的 role、Skill、意图、MCP 组件已经通过 resolvedesk/services.py 接入当前服务。
摘要、画像、语义历史、工单、任务检查点和评测保存到 SQLite，本机运行无需 Redis 或 Docker。
兼容入口 api/main.py 与 memory/conversation_memory.py 仍保留独立 Redis/Chroma 路径，不是当前主入口。

只使用 deepseek-flash；累计预算 20 元，保留 2 元余量。费用账本位于 data/billing/usage.sqlite3，不要删除。
本轮测试结果与说明在 reports/。Docker、企业 SSO、外部工单平台和生产级部署不在当前验收范围。

修改前版本在原文件旁 history/时间戳.唯一标识/原文件名；原始资料和所有历史均保留。