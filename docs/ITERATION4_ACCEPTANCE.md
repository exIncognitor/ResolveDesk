# 第四轮交付与验收

目标：[ITERATION4_GOAL.md](ITERATION4_GOAL.md)。基线 `889d3c10463cbe720bd569b971eb884b89cb5b52`；真实 Flash 与部署运行代码 `66a68a5daa5f2f822b8b78dd1e01c89fee8fdf74`。后续交付提交只补报告和文档。当前架构入口：[CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md)；三条模拟流程和讲解：[BUSINESS_DEMOS.md](BUSINESS_DEMOS.md)。

## 审查问题闭环

| 问题 | 实际修改 | 验证 |
| --- | --- | --- |
| RAG 充分性误挡问候、本人业务读取和紧急升级 | 服务端按事实源应用门禁；知识建议仍要求有效依据 | 原基线受控失败，候选定向通过；真实两轮覆盖资产、工单、问候及升级 |
| 模型遗漏或改错 workflow 导致专用申请退化 | 字面同意和服务端任务契约独立于路由；关键字段来自用户；缺信息可靠保存 awaiting_input | 3 种流程 × 3 种错误参数组合；确认后检查真实 service_case；真实权限、维修、席位闭环 |
| 无元数据新导入绕过治理 | 所有新导入先进入草稿；锁定迁移只登记存量精确版本，支持 revision=0 纳管；补元数据表单和审核入口 | 迁移、导入、缓存、ACL、Chroma、MCP 与当前 API 回归；浏览器隔离样例从待补信息到发布 |
| suite 身份遗漏发布信息 | 发布状态、内容版本和规范化语义元数据纳入 suite；源码身份单列 | 元数据变化改变 suite，纯引擎变化不伪装成测量输入变化；冻结恢复保留发布记录 |
| 文档与当前架构不一致 | 建立唯一当前入口，将早期 SQLite/规则路由说明标为历史 | 当前文件/函数导读、技术取舍和三条演示已交付 |
| 同步 SQL 阻塞事件循环 | 先测 Store 热点，再局部卸载 SSE 每轮读取和主 Agent 工单读取 | 受控慢 SQL 三次重复，确定性协程并行回归，保留未完全异步数据库的限制 |

另修复已提交会话无法继续查询的问题：API、Vue 和 CLI 可在原会话查询进度；存储层禁止在该会话准备第二张工单，重复原确认仍返回同一工单。未提交申请取消会清除状态；已提交工单的聊天取消不会冒称撤销资源。字面取消优先于错误推断的路由分类。

真实服务回归还发现初始化治理标记被迁移工具当成“目标非空”，已改为仅允许经核验的空目标初始化标记；迁移既有 marker 与受管理草稿时保留原始语义，不自动放行新材料。

## 自动化和界面验证

- 基线有效受控回归：16/16 失败、0 错误，报告 `reports/iteration4-baseline-contracts.json`。初次 scratch 父目录准备错误另存本机，未计作功能失败。
- 整合阶段真实 PostgreSQL/Redis/Chroma：456 项和 14 子测试通过，见 `reports/iteration4-final-services-v1.xml`。随后增加路由误判下取消回归，含最终状态调整的定向 46 项通过，见 `reports/iteration4-final-state-v2.xml`。最终 GitHub 全量检查覆盖该调整。
- Vue 35 项通过并构建；已提交会话查询、发布信息校验和元数据 revision=0 均有回归。
- 浏览器使用单独的 SQLite 模拟库和非秘密测试身份，实际完成缺信息不可发布、保存元数据仍为草稿、审核后可检索。截图 `reports/iteration4-ui-publication.jpg`。测试服务已停止并清理本次两个临时文件；没有向公共知识库发布该样例。
- 早期整合失败报告 `reports/iteration4-services-tests.xml` 与随后修复的 44 项报告仍保留，不替换成成功结果。
- `pip check`、源码发布扫描通过。依赖扫描为 `pass_with_profile_exclusions`：保留先前核验的四项 Chroma 部署画像排除，2026-11-26 到期；这不是上游漏洞已修复或零已知漏洞。

受控 `pg_sleep(.12)` 下，三次重复的最大心跳间隔中位数：SSE 141→31ms，Agent get_ticket 188→47ms。原始报告为 `reports/iteration4-baseline-eventloop.json` 和 `reports/iteration4-candidate-eventloop.json`。这是慢驱动故障实验，不是正常请求吞吐提升；没有全面重写同步 SQL 存储层。

## 最终真实 Flash 验收

固定合同 `evaluation/iteration4_contract.json` 在修改前冻结 12 组合成场景、16 轮输入及 40 个输入文件哈希。两轮均使用同一最终代码、相同 suite 身份、相同原始知识片段与发布元数据：26 篇官方技术参考 + 8 篇原始模拟制度，共 34 篇、62 片段。通过只读事务取得公共知识，再按原字段/版本复制到独立 schema/Chroma/Redis，未重新导入改写正文或标题。

| 指标 | 第一次 | 第二次 |
| --- | --- | --- |
| 确定性硬指标 | 12/12 | 12/12 |
| 含辅助 Judge 的严格整组通过 | 9/12 | 9/12 |
| 执行/评分错误 | 0 | 0 |

硬指标检查逐轮状态、明确实体、实际工具、确认前后工单集合、精确 workflow 字段、幂等确认及真实模拟业务落库效果。权限实际写入有期限的 grant；维修完成返工和本人验收并释放资产；席位实际预占及分配。Judge 阈值与原始分数未改。

两次 Judge 低分均为 `v4-cancel-intake`、`v4-unsupported-policy`、`v4-critical-handoff`：取消前的必要信息追问帮助度偏低；未知企业报销制度返回通用四类业务追问，表达缺少针对性；紧急交接引用安全材料并说明未接真实人工队列，但完整性/帮助度低。保留这些质量缺口，不把硬指标全通过写成答案质量全通过。

原始报告 `reports/iteration4-candidate-v1-live-r1.json`、`reports/iteration4-candidate-v1-live-r2.json`，摘要 `reports/iteration4-summary.json`。本轮没有付费基线重跑，旧第二/三轮报告属于历史版本和不同语料/合同，不能直接用来计算这次效果提升。合成小样本、同一 Flash 生成和评分，也不能代表第三方盲测或生产泛化。

## 部署、费用与恢复

宿主机 8080 两个 worker、Docker 18080 已更新到运行代码 `66a68a5`。`reports/iteration4-deployment-20261028T033820668235Z.json` 30/30，逐文件源码哈希、依赖版本、JS/CSS、34 篇可读发布材料、Prometheus 均匹配。两入口 API/CLI 各 11/11、标准 MCP SDK 各 11/11；Docker 评测保留探针 6/6，只清理其本次新建的免费合成评测，并先建立全库备份。部署检查均未新增付费调用。外部告警 Webhook 继续按用户选择不配置，真实外部送达未验收。

真实调用新增 0.902674 元，累计 9.767250 元、2113 次 settled、无未决预留。本轮独立 3 元上限满足；原 20 元累计上限保留 2 元后还有 8.232750 元可用。评测进程使用更严格总额 13.864576/保留 2 元，没有重置或恢复消费。

切换前先检查无公共活跃任务并停旧容器应用。全库备份：`.codex-runtime/checkpoints/eval-retention-20261028T033730732009Z-8e5d201e/resolvedesk.dump`；SHA-256 `15abf3e6d9fb722d9cb7f91578fff4d85fdba82aa00ba530b21da34cb77d4490`，已由 `pg_restore -l` 验证。文件含敏感本地数据，不上传。评测探针生成的额外备份亦保留。

代码恢复点为本地 Git 提交；没有生成相邻 history。原始冻结资料、真实评测隔离资源和此前受限清理目录保留。恢复不能只切回不理解治理状态的旧代码；需明确选择应用数据与代码，并始终保留恢复时的真实 `resolvedesk_finance` 消费，禁止整库回滚费用。

最终交付推送到既有私有仓库 `exIncognitor/ResolveDesk`。精确交付 SHA 和 Windows Python、Linux Python、Vue、三服务四项 CI 结论保存到本机 `.codex-runtime/iteration4-ci-final.json`；以该文件及 GitHub 对应 SHA 的实际结论为准。
