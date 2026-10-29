# 首次启动指南

[返回首页](../README.md) · [三条业务演示](BUSINESS_DEMOS.md)

准备 Python 3.12、Node.js 22.12+、Docker Desktop 或 Docker Engine + Compose。以下流程面向新克隆的工作目录；已有数据的升级入口见文末。

## 1. 安装 Python 依赖

Windows PowerShell：

```powershell
git clone https://github.com/exIncognitor/ResolveDesk.git
cd ResolveDesk
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

Linux / macOS：

```bash
git clone https://github.com/exIncognitor/ResolveDesk.git
cd ResolveDesk
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

下文的 `python` 都指激活后的虚拟环境。若 PowerShell 不允许激活，可将每个 `python` 换为 `.\.venv\Scripts\python.exe`。

## 2. 初始化数据库、缓存与向量服务

在项目根目录执行：

```powershell
python -X utf8 -B tools/setup_services.py
docker compose --env-file .codex-runtime/services.env -f compose.services.yaml up -d --wait
python -X utf8 -B tools/check_services.py
python -X utf8 -B tools/setup_local.py --download-model
python -X utf8 -B tools/setup_services.py
```

- 第一次 `setup_services` 生成本机服务配置和随机密码。
- `setup_local` 创建 learner/support 两个模拟身份、导入模拟业务和知识，并下载固定版本的 multilingual-e5-small ONNX 模型。初次下载需要联网。
- 第二次 `setup_services` 为 Prometheus 准备专用本地访问文件。

默认端口：PostgreSQL 15432、Redis 16379、Chroma 18001。出现端口占用时，在本机 `.codex-runtime/services.env` 中调整对应端口后核对配置。

## 3. 配置模型、服务地址和密钥

新目录可从 `.env.example` 创建本地 `.env`：

```powershell
Copy-Item .env.example .env
```

Linux/macOS 使用 `cp .env.example .env`。已有 `.env` 时编辑原文件。

在服务端填写模型协议、模型名称、服务地址和自己的密钥。默认示例使用 DeepSeek Flash 的 Anthropic 兼容接口：

```dotenv
RESOLVEDESK_MODEL_PROTOCOL=anthropic
RESOLVEDESK_MODEL=deepseek-flash
RESOLVEDESK_MODEL_BASE_URL=https://api.deepseek.com/anthropic
RESOLVEDESK_MODEL_API_KEY=<填写自己的密钥>
```

主循环、专业节点、记忆和评测使用配置的模型。正常运行不需要费用账本，也没有累计额度开关。OpenAI Chat Completions 兼容服务、旧变量兼容与切换规则见 [模型配置](MODEL_CONFIGURATION.md)。

## 4. 构建界面并启动

```powershell
npm --prefix frontend ci --no-audit --no-fund
npm --prefix frontend run build
python -m uvicorn resolvedesk.app:app --host 127.0.0.1 --port 8080 --workers 2 --no-access-log
```

Windows 也可执行 `.\tools\start_local.ps1 -Workers 2`。打开 http://127.0.0.1:8080 ，依赖就绪检查为 http://127.0.0.1:8080/ready 。

在“连接配置”中，地址填 `/`，访问令牌从本机 `.codex-runtime/access.json` 对应账号的 token 读取：

| 账号 | 用途 |
| --- | --- |
| learner | 本人申请、查看资源、维修验收、席位回收 |
| support | 审批和办理、知识管理、评测与监控 |

这里填写本应用访问令牌。供应商 API Key 留在服务端 `.env`。

## 5. 发布内置知识，再体验服务

首次导入的内置知识是 draft。使用 support 进入“知识库”，补齐材料类型、适用范围、来源核对日期与有效期，保存后审核发布。内置企业政策属于模拟制度；技术参考资料按其实际来源填写。完整表单说明见 [知识发布操作](KNOWLEDGE_PUBLICATION.md)。

随后切换 learner，从首页的账号权限、设备维修、软件授权或网络排障开始。快捷入口只填好问题，按发送后才会实际调用模型。三条完整流程见 [业务演示](BUSINESS_DEMOS.md)。

## 完整容器部署

完成上述本地初始化，停止宿主机应用后，可启动包含前端、应用、网关、PostgreSQL、Redis、Chroma、Prometheus 的整套服务：

```powershell
docker compose --env-file .codex-runtime/services.env -f compose.yaml up -d --build
```

工作台 http://127.0.0.1:18080 ，Prometheus http://127.0.0.1:19090 。容器通过既有 data 挂载保留模型与身份，通过本地配置连接共享服务和费用权威记录。Nginx 提供同源页面与 API。

## 常见检查

| 现象 | 定位入口 |
| --- | --- |
| 页面打开但无法读取本人记录 | “连接配置”中的地址、访问令牌及 `/me` |
| 技术路径正常但依赖失败 | `/ready`、`tools/check_services.py`、Compose 服务状态 |
| 知识导入后搜不到 | 当前文档是否审核发布、生效时间、ACL、索引状态 |
| 模型调用失败 | 服务端协议、模型名、地址、密钥以及服务商返回的错误 |
| 改了Vue源码页面没变 | 重新构建frontend并刷新页面；容器入口还需重新构建应用镜像 |
| API使用说明 | `/docs` 和 [API与CLI指南](API_CLI_MIGRATION_2026-10-27.md) |

## 已有安装的升级

已有业务、模型和累计费用时，保留本地数据与配置，使用对应切换文档：[业务存储迁移](SERVICE_CUTOVER_2026-10-27.md)、[记忆与费用迁移](MEMORY_MIGRATION_2026-10-27.md)。代码版本可用Git恢复；数据和费用另行备份，已发生的费用不随代码回滚。

[返回首页](../README.md)

