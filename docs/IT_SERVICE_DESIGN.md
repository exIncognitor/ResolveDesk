# ResolveDesk IT 服务台：实现与验收边界

> 历史阶段文档：以下正文保留早期 SQLite、规则路由与尚未加入席位库存时的设计和验收记录，不代表当前架构。当前唯一架构入口为 [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md)，业务演示见 [BUSINESS_DEMOS.md](BUSINESS_DEMOS.md)。

更新：2026-10-27。主入口为 `resolvedesk.app:app`，业务数据均为明确标注的模拟企业数据。

迁移完整性以 [技术迁移总审计](MIGRATION_AUDIT_2026-10-27.md) 为准。本说明描述本地已实现流程；“完整质量评测”等章节名称不代表原版所有评测、路由、监控子能力都已迁移。

## 四类服务

| 场景 | 知识与规则 | 可执行范围 |
| --- | --- | --- |
| 账号权限 | 账号锁定、重置、最小权限、资源目录、期限、审批、审计 | 查询本人有效权限，准备权限申请，确认后审批/驳回/模拟开通/取消 |
| 网络与终端 | VPN 691/超时、DNS、无线断连、蓝屏、磁盘风险、安全升级 | 引用知识排障、标准 MCP 只读诊断清单、普通 network 工单；不修改真实终端 |
| 软件授权 | 白名单、IDEA 企业席位、激活、到期、回收与采购边界 | 查询模拟静态目录与席位、解释制度、普通 software 工单；未实现厂商许可证分配或库存扣减 |
| 设备服务 | 本人资产、保修、危险故障、维修、验收、返工、借用规则 | 维修草稿、确认、分诊、派单、处理、验收/返工/取消；更换借用仅提供制度说明和普通工单 |

7 篇随项目知识文档首次完整导入后为 32 个片段；用户自行导入或修改会改变数量。`knowledge/` 为合成制度，`BusinessService.context()` 提供模拟资源、软件目录、本人资产和有效权限。

## 实际调用链

`Vue → POST /runs → DeskAgent → 权限过滤的混合检索 → DeskServices.route → 必要时并行专业节点 → Flash 工具循环 → 草稿 → 申请人确认 → SQLite 事务 → 业务状态接口`。

保留 general / technical / billing / escalation 四个兼容角色 ID，职责分别调整为账号分诊、网络设备诊断、软件授权、安全与人工升级。当前 IT 场景分类使用版本化规则；原意图识别组件仍提供实体与紧急度信息。复合请求确实执行多个只读专业节点，再由主 Agent 汇总。旧电商角色模板的专属输入字段不进入当前专业节点的输入包。

模型只拥有读工具、澄清工具和草稿工具。模型无法确认工单、审批、开通权限或改变维修状态；这些操作分别由已登录申请人或 support 调用鉴权接口。

## 权限申请

`pending_approval → approved → fulfilled`，也可以在待审批阶段 `reject → rejected` 或由本人 `cancel → cancelled`。

- 资源目录目前为 repo-resolvedesk（read/write）、docs-internal（read），期限 1–30 天。
- 确认提交时再次检查字段和政策；申请人不能自批，普通用户不能审批。
- approve 只改变审批状态，provision 才实际写入本地 `desk_grants`。有效期从开通时计算，到期记录不再作为有效权限返回。
- 所有状态变化、审计、幂等响应在同一 SQLite 事务中保存。每次操作带当前版本，旧页面的并发操作返回 409。
- 这是本地模拟授权，没有调用 GitHub 或任何真实 IAM 系统。

## 设备维修

`submitted → triaged → assigned → in_progress → resolved → closed`；申请人在 resolved 可 reopen 回 in_progress；submitted 可由本人取消。

- 只能报修本人资产。一个资产只能有一条未完成的维修；同一确认重复执行仍返回同一个工单。
- IT 操作员分派给自己，只有被分派者可以开始处理和提交结果；申请人验收，不允许 IT 代替申请人验收。
- 待验收仍占用资产；关闭或取消才释放。返工继续占用同一资产和工单。
- 演示数据：learner 的 LT-1001 在保；support 的 LT-1002 过保。过保不自动扣费。

## 完整质量评测

`POST /eval/run` 接受 `{"kind":"quality","limit":6,"use_judge":true}`；返回持久化任务 ID，使用 `/eval/jobs/{id}` 查询。免费检索回归使用 `{"kind":"retrieval"}`。

质量评测通过隔离的 ASGI 应用执行当前 HTTP 入口，使用同一 DeskAgent、服务层、鉴权与事务代码；不会改动主库的工单、资产或权限。只复制租户共享知识，不复制私有文档、真实历史对话和业务记录。隔离数据库留在 `.codex-runtime/quality-<任务及尝试ID>/`，用于核验。

- 版本化语料 `evaluation/it_service_cases.json`：28 条意图分类、6 组对话（共 8 个用户回合）。均为人工编写的合成开发集。
- 检索集 `evaluation/retrieval_cases.json`：20 条固定题，另加经人工审核的负反馈回归题。
- 对话检查状态、路由、规范文档召回与引用、工具执行、先澄清后补齐、确认幂等、业务字段、最终授权内容与期限、维修资产释放。
- 用例中的参考答案与期望字段由评测器持有，不作为被测 Agent 的上下文。审批与维修操作由评测脚本扮演 IT 操作员，不冒充模型自主审批。
- 复用四维 LLM Judge 与五条评分校准例，Flash 评分阈值 0.75。评分失败单独记录，从有效评分分母排除；不能把错误当成零分或满分。
- 人工选择完整通过的有效报告设为基线：`POST /eval/jobs/{id}/baseline`。用例、知识版本、模型、Judge 提示与阈值一致才比较；指标相对下降超过 5% 标记回归，不自动覆盖基线。
- 重启将未完成评测标为 paused；界面能查看任务记录并显式重新执行。重新执行使用新的隔离目录，可能再次消耗模型费用。

这些样本用于开发回归，不是生产样本或独立盲测；同一个 Flash 同时生成与评分存在偏差。没有继承原简历模板中的 500 条数据及对应指标。

## 监控与告警

support 可查看 `/monitor` 与鉴权 `/metrics`（Prometheus 文本）。普通用户只看到本人请求统计。

- 主执行、专业节点、工具成功/失败和耗时、检索降级来自实际执行记录，持久化到 telemetry。
- 默认 300 秒窗口、至少 3 条样本；失败率超过 50% 或 P95 超过 30 秒触发告警。主动暂停记录为中断，不作为角色失败。
- 告警具有 active/resolved 生命周期；状态变化才产生通知事件，重复读取不重复入队。
- 专业节点失败告警将该角色降权并转通用/升级节点；新窗口恢复后清除监控降权。鉴权与审批规则不随降级放宽。
- 通知 outbox 持久化。配置服务端环境变量 RESOLVEDESK_ALERT_WEBHOOK 后，每次投递包含稳定幂等 ID，失败间隔 30 秒、最多 3 次；接收端应据 ID 去重。不跟随重定向。
- 未配置地址时不向外发送，事件保存在本地。真实本地 HTTP 接收端已用于验证触发和恢复投递；未连接真实飞书/企业告警群。

## 运行与验证

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B tools/setup_local.py
.\tools\start_local.ps1
```

浏览器打开 http://127.0.0.1:8080，连接配置中填本机 `.codex-runtime/access.json` 的 learner 或 support 令牌。DeepSeek Key 留在服务端，不填到页面。业务服务台提供状态操作，评测页提供任务记录、免费检索及付费质量评测。

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m pytest -q
.\.venv\Scripts\python.exe -X utf8 -B tools/check_release.py
```

前端在 frontend 下执行 `npm test`、`npm run build`。有限真实验收用 `tools/accept_it_service.py --quality`；`--live-workflows` 会在主库新增明确标注的模拟申请和维修。API 使用原共用 20 元账本并保留 2 元余量，仅允许 deepseek-flash。

首轮报告保留了 4/6 的质量失败（业务流程已成功，但草稿回复信息不足）；之后补充真实草稿内容和审批边界并重新验收。不要只挑选通过结果而抹除失败记录。

## 当前边界与恢复

本轮完成的是本地 Python/Vue IT 服务闭环。未接企业 SSO、IAM、CMDB、厂商授权或真实维修平台；没有分布式压测和生产部署证明。完整旧画像提炼、通用外部 MCP 配置、Redis/Chroma 兼容入口仍属于另行验收范围。Docker 按要求跳过。

源码修改按 Git 检查点留存，不生成 history。数据库及费用账本的改造前一致性快照位于 `.codex-runtime/checkpoints/20261027T022745Z/`，首轮验收后的快照位于 `20261027T023150Z/`；均已通过 SQLite integrity_check，忽略于 Git。恢复数据库会影响快照之后的业务和费用数据，应单独确定恢复范围，尤其不能回退费用账本后继续消耗 API。
