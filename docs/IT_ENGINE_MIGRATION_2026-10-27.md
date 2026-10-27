# G01–G04：当前 IT 识别、状态、编排与专业工具

本阶段对齐固定审计的G01–G04，完整G01–G13目标继续保持active。G08全集评测、G09完整监控、G10完整轨迹等不因本阶段通过而视为完成。

## 当前实际链路

`resolvedesk.app → DeskAgent._bootstrap → DeskServices.route → ITIntentRecognizer + EntityState → 检索/记忆 → DeskServices.consult → AgentOrchestrator._execute → IT专业节点 → 协调工具循环/只读交接 → 用户确认事务`。

普通请求和复合请求均走同一个专业执行器。专业节点只有只读工具；模型准备的草稿仍由协调器核对，创建/审批/开通/维修状态写入仍需既有显式用户/客服接口。

| 包 | 实现与验收 |
| --- | --- |
| G01 | `intent.py`：IT细粒度标签映射4类业务与分诊/升级；Flash、固定E5、规则按0.7/0.2/0.1融合，缺失通道显式记录并重新归一权重，规则不会再覆盖融合结果。分数是融合证据，不声称校准概率。低置信或unknown在持久状态中追问；CRITICAL进入安全交接。 |
| G02 | `entity_state.py`：独立于草稿保存实体、来源turn_seq、任务代次。支持省略字段继承、明确纠错、换资产/资源/任务后重置；Literal用户字段覆盖模型陈旧参数。未决多资产/资源/期限不默认取最后一项；单对象草稿必须先澄清。 |
| G03 | `services.py`、`agent.py`、`roles.py`：普通/复合统一派发，实例选优/冷却探测/取消释放、失败回退、实际角色、partial/failed；每个完成节点与新增来源保存检查点，暂停恢复复用成功结果。安全交接直接展示授权原文和真实人工渠道边界，不进入未请求的建单追问。 |
| G04 | 显式ITGeneral/ITTechnical/ITLicense/ITEscalation类，不再靠给旧Billing换Prompt。共享RAG/引用读取、当前用户业务目录、软件许可核验、本人资产、申请字段、交接摘要和原确定性技术诊断均实际接线。MCP目录变动会更新专业工具；工具没有可由模型传入的身份字段。 |

## 向量提供方

当前主入口默认`RESOLVEDESK_INTENT_VECTOR_PROVIDER=local_e5`，复用固定`Xenova/multilingual-e5-small@761b726dd34fb83930e26aab4e9ac3899aa1fa78`的CPU ONNX模型。没有因本机环境改成n-gram替代。

可选`self_hosted_http`通过`RESOLVEDESK_INTENT_VECTOR_URL`和专用`RESOLVEDESK_EMBEDDING_TOKEN`连接`resolvedesk.embedding_service:app`。令牌至少32字符，由服务启动环境提供；不写源码、不打印，不会读取旧第三方embedding Key。该端点使用`/v1/embeddings`并显式区分query/passage，不声称兼容所有第三方embedding扩展。

服务需先有本地固定模型，运行示例为`python -m uvicorn resolvedesk.embedding_service:app --host 127.0.0.1 --port 18002 --no-access-log`。启动环境需包含上述专用令牌。HTTP模板与查询始终使用同一模型/提供方，响应检查模型ID、条数、索引、维度、有限值和范数，连接/请求有界，无隐式模型下载或付费API。

`test_http_embedding.py`已启动真实独立HTTP/E5进程，验证与本地384维向量逐元素误差小于1e-6、无令牌401，并通过真实DeskServices配置完成HTTP向量到实际路由的断言。

## 动态证据

- 全量Python **247 passed + 14 subtests**；前端 **26 passed**且构建成功。包含真实PostgreSQL/Redis/Chroma、MCP子进程和本地模型HTTP服务。
- [真实8场景最终报告](../reports/it-migration-live-20261027T055532871350Z.json)：网络、许可、双专业复合、安全升级、unknown澄清、7天权限草稿、纠错14天、切换设备维修，8/8通过。每个实际专业执行最终状态均为success；不等于生产准确率证明。
- [两个真实Flash节点自主工具报告](../reports/it-migration-live-20261027T055055426882Z.json)：technical和billing都实际调用RAG与标准MCP，billing另调用IT业务目录/许可核验，2/2通过，原始工具轨迹保留在报告中。节点重复的只读调用如实记录，不伪装成只有一次。
- 首次[8场景报告](../reports/it-migration-live-20261027T054633355682Z.json)仅基础断言通过；复核发现电池鼓包回复先追问维修资料。随后加强即时安全指引断言并修复路径，保留旧报告作为发现过程。专业输出也由700 token改为1200上限并要求简短，最终复合/安全节点未再因截断标partial。
- 故障测试覆盖普通/复合专业失败、实际General回退、partial保留、全部失败不让协调器编造、未知直接澄清、节点探测互斥与取消释放、跨用户资产/私有知识拒绝、暂停恢复不重复已完成专业调用及其新增来源。
- 前端按原turn_key取回旧轮次追踪，显示实际执行角色和partial。人工升级显示“需人工核验”，不声称真实队列已接通。追踪暂时失败不会丢掉已完成的付费回答或捏造general/0置信度；完整耗时和轨迹分页仍归G10。

上述隔离验证实际Flash保守记账增加**0.653850元**。另外在已部署Docker网关执行1次明确标注“合成部署验证”的unknown澄清，耗费**0.000924元**，仅保存测试对话，没有创建或改变业务记录。当前累计**1.599820元**、339次settled，仍保留2元安全余量；未决预留为0。

8场景及专业工具验证使用新建的独占合成PostgreSQL schema和Chroma集合，证据保存后已删除独占测试数据。主库保留上述一条有明确标记的部署测试对话，便于在工作台查看。

## 当前部署证据

镜像`resolvedesk-app:e5c3d05`与宿主机8080两个工作进程均已更新，Docker网关仍为18080。[部署报告](../reports/it-engine-deployment-20261027T062345120418Z.json)核对运行容器10个关键源码SHA-256与已验工作区一致，`/system`实际报告ITIntentRecognizer、固定E5、四种显式IT类及当前工具列表。Docker内实际Flash调用成功，LLM/E5通道均可用，只有预期的一次付费调用。

[宿主机检索](../reports/services-runtime-20261027T062348005090Z.json)与[Docker检索](../reports/services-runtime-20261027T062206676119Z.json)各60/60，未调用模型；Vue新构建静态资源200，Prometheus up=1。没有把这些有限验收推断为生产质量、全集识别准确率或完整G01–G13迁移结论。

## 恢复与剩余范围

源码按项目规则保存Git检查点；新版本切换前的PostgreSQL public schema与累计费用账本一致性备份位于`.codex-runtime/checkpoints/g01-g04-20261027T061318071976Z-7c99ea51/`。原账本持续累加，不从备份倒退。

本阶段不声称G05完整记忆、G06统一缓存/熔断、G07外部MCP服务端/配置、G08完整Judge和自定义集、G09完整监控、G10完整轨迹、G11导入/CLI已完成。进度以`MIGRATION_PROGRESS.md`和当前实际入口为准。
