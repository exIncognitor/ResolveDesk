# 模型配置与身份工作区

[项目首页](../README.md) · [首次启动](GETTING_STARTED.md)

## 默认模型与支持后台

员工通过账号密码登录，没有模型管理入口，也无权调用`/admin/models`、费用、账号或审计接口。support进入“支持后台 → 模型与用量”，填写配置名称、协议、模型ID、服务地址、API Key和可选单价。支持保存多套配置并切换；默认环境配置也可以一键切回。

服务端把Key加密存入PostgreSQL，浏览器只能看到“已配置”。编辑时Key留空会保留原值。编辑当前配置或切换模型只影响新任务，已开始的任务固定原配置；不同worker都从同一数据库读取下一次任务的配置。管理动作写入审计。

`.env`提供未选择后台配置时的默认模型：

在服务端项目根目录的 `.env` 中配置：

```dotenv
RESOLVEDESK_MODEL_PROTOCOL=anthropic
RESOLVEDESK_MODEL=deepseek-flash
RESOLVEDESK_MODEL_BASE_URL=https://api.deepseek.com/anthropic
RESOLVEDESK_MODEL_API_KEY=<填写自己的密钥>
```

支持 `anthropic` Messages 和 `openai` Chat Completions 两种文本/函数工具协议。OpenAI兼容服务示例：

```dotenv
RESOLVEDESK_MODEL_PROTOCOL=openai
RESOLVEDESK_MODEL=<服务商提供的工具调用模型名称>
RESOLVEDESK_MODEL_BASE_URL=https://your-provider.example/v1
RESOLVEDESK_MODEL_API_KEY=<填写该服务商的密钥>
```

将示例地址与名称替换为实际服务商配置。OpenAI地址填写API根地址，例如带 `/v1` 的地址；也接受以 `/chat/completions` 结尾的完整地址。当前适配不包含Responses API、多模态和供应商专有推理强度参数。

旧配置 `ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL` 仍可作为兼容回退；新的`RESOLVEDESK_MODEL_*`配置优先于旧变量，后台选定的配置优先于环境默认。改`.env`需要重启进程或重新创建容器；后台切换不需要重启。主循环、专家、记忆、检索优化与质量评测均接入配置化模型及持久用量记录。

support可以在后台输入自己的Key；保存后字段清空，后端不返回密钥原文。加密主密钥默认位于`data/config-encryption.key`，也可从`RESOLVEDESK_CONFIG_ENCRYPTION_KEY`注入；必须独立备份。生产模式编辑模型前须配置服务端`RESOLVEDESK_MODEL_ALLOWED_HOSTS`域名清单，远程服务使用HTTPS。

## 运行限制与用量

正常调用不设置累计人民币额度，不做费用预留，不依赖费用账本或历史财务迁移。原开发期开关和20元限额不影响当前运行链。

保留单次执行超时、工具步数与请求中的输出Token上限，控制任务执行范围。SDK自动重试保持关闭。模型供应商仍按实际服务计费。

`/admin/model-usage`按租户从PostgreSQL汇总实际调用、输入/输出/缓存Token和分币种估算费用，重启不会清零。每次请求先记录`started`，收到usage后结算为`completed`；失败记为`failed`。崩溃或结算暂不可用时保留未知记录，不自动重放请求。

单价单位为每百万Token。OpenAI的prompt_tokens通常包含缓存命中，需先减去缓存再计算普通输入；Anthropic普通input_tokens不包含缓存读，分别计价。费用按每次请求时的单价快照计算，修改价格不会改写旧记录。缺单价、缺usage、失败或当前未定价的缓存写入均显示费用未知，不伪装成0；CNY与USD不相加。页面不查询供应商账单、余额，也不把估算当实际扣款。最多汇总最近10000条并显式标记截断。

原费用表、旧SQLite和验收报告保留。core/paid_api.py、core/financial_ledger.py只供历史开发实验与迁移工具复现，正常服务不导入它们。

## 切换模型的状态边界

新的Agent检查点保存脱敏模型身份；恢复时模型、地址或协议改变会拒绝混合执行。更换密钥但保留同一服务和模型不改变模型身份。模型身份也纳入检索优化缓存、评测快照与报告，评测中途换模型需新建任务。

当前两协议通过模拟HTTP/SDK传输测试；已有真实端到端结果来自Flash。其他模型需要具备函数工具调用能力，并分别核验其参数和输出兼容性。

### OpenCode 免费模型核验（2026-10-29）

本地可另存 `OPENCODE_ZEN_API_KEY`，不会自动替换当前模型的密钥。若服务商允许接入，将其值填入 `RESOLVEDESK_MODEL_API_KEY`，协议选 `openai`，地址为 `https://opencode.ai/inference/openai/v1`，模型填服务商的实际ID。

使用应用的普通HTTP适配器和纯合成输入分别请求了 `beam-free`、`big-pickle`：前者返回403 `RegionError`，后者返回429 `FreeUsageLimitError`。两者尚未进入函数工具和流式验证，不能标记为可用。当前服务仍使用原DeepSeek配置，未自动回退到其他模型或发起新的收费测试。证据见 [真实请求报告](../reports/free-model-probe-20261029.json)。

[OpenCode官方说明](https://opencode.ai/v2/docs/console/models/)列出免费模型的客户端、地区与额度限制；零价格不等于任意第三方应用均可使用。可用性以自己账号的实际响应为准。

## 身份与功能入口

| 身份 | 主要功能 |
| --- | --- |
| user（演示账号learner） | 对话、可读知识查询、本人历史与记忆、本人资源和业务申请、本人验收、授权撤销与席位回收 |
| support | 以上能力，以及租户内业务审批/派单/转派/处理、知识治理、评测、监控、模型/Key/单价/用量管理、账号启停和会话撤销 |

账号密码通过`/auth/login`换取8小时会话；数据库只保存密码的PBKDF2摘要与会话Token摘要。每次HTTP和知识MCP请求都核验账号启用状态、会话有效期和撤销代次。退出撤销当前会话，后台可撤销账号全部会话；连续失败会暂时锁定该账号/来源组合，Nginx额外限制登录请求频率。

浏览器会话仅保存在当前标签页的sessionStorage，角色从`/me`重新核验；切换账号清空旧工作区，迟到响应不能恢复管理入口。测试用快速登录只有`RESOLVEDESK_DEMO_LOGIN_ENABLED=true`且环境为development/test才启用；生产模式在服务端强制禁用，不能靠调用隐藏接口绕过。

support可以处理租户内业务工单，他人的聊天与私有记忆仍按本人范围隔离。按当前范围保留两种角色，不再细分管理员与审批人，因此support应授予受信任的IT管理人员。已实现后台创建账号；没有公开注册、邮件找回密码或企业SSO接入。

负反馈候选及其转回归接口只允许support处理本人的会话；跨用户、跨租户的会话不能因为负反馈而暴露。MCP连接管理状态仅support可读；员工仍可通过标准只读知识MCP查询本人有权阅读的知识。企业落地前的职责分离、身份生命周期及真实系统接入缺口见 [企业使用边界检查](ENTERPRISE_READINESS.md)。

[返回文档中心](README.md)

