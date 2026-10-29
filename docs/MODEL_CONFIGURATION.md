# 模型配置与身份工作区

[项目首页](../README.md) · [首次启动](GETTING_STARTED.md)

## 配置模型服务

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

旧配置 `ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL` 仍可作为兼容回退；新的 `RESOLVEDESK_MODEL_*` 配置优先。调整后重启宿主机应用或重建并启动容器。主循环、专家、记忆、检索优化与质量评测使用配置的模型。

密钥仅在服务端读取，不放入浏览器。support 可从“任务与连接 → 模型接入”查看协议、模型、地址和密钥是否已配置；接口不会返回密钥原文。

## 运行限制与用量

正常调用不设置累计人民币额度，不做费用预留，不依赖费用账本或历史财务迁移。原开发期开关和20元限额不影响当前运行链。

保留单次执行超时、工具步数与请求中的输出Token上限，控制任务执行范围。SDK自动重试保持关闭。模型供应商仍按实际服务计费。

`/system` 的 `usage` 记录当前客户端worker观测到的Token与成功/失败调用计数，缺失usage单列，不计算价格。它在进程重启时清空，不是全实例持久财务统计。

原费用表、旧SQLite和验收报告保留。core/paid_api.py、core/financial_ledger.py只供历史开发实验与迁移工具复现，正常服务不导入它们。

## 切换模型的状态边界

新的Agent检查点保存脱敏模型身份；恢复时模型、地址或协议改变会拒绝混合执行。更换密钥但保留同一服务和模型不改变模型身份。模型身份也纳入检索优化缓存、评测快照与报告，评测中途换模型需新建任务。

当前两协议通过模拟HTTP/SDK传输测试；已有真实端到端结果来自Flash。其他模型需要具备函数工具调用能力，并分别核验其参数和输出兼容性。

## 身份与功能入口

| 身份 | 主要功能 |
| --- | --- |
| user（演示账号learner） | 对话、可读知识查询、本人历史与记忆、本人资源和业务申请、本人验收与席位回收 |
| support | 以上能力，以及租户内业务审批/派单/处理、知识导入与审核发布、Skills重载、质量评测、监控与模型接入状态 |

访问令牌经服务端 `/me` 解析身份。前端把角色绑定到当前令牌和服务地址；切换账号时旧身份立即失效，迟到响应不能恢复管理入口。服务端仍逐项检查角色、租户、本人范围和业务状态。

support 可以处理租户内业务工单；他人的聊天与私有记忆仍按本人范围隔离。当前采用两种角色，账号来自本地初始化文件，并没有新增注册、密码找回或企业SSO系统。

[返回文档中心](README.md)

