# G11 API 与 CLI 迁移

比较范围为原Python的17个装饰器HTTP接口，以及另计的标准MCP协议和交互CLI。`resolvedesk.app:app` 是当前服务；`api.main`保留兼容代码，不能用旧入口证明新实现完成。下表是当前去向；实现已有定向行为测试，最终部署结果由同批报告记录。

| 原接口 | 当前入口 | 契约及破坏性变化 |
| --- | --- | --- |
| GET /health | GET /health、/ready、/system | 健康/真实依赖/详细状态分别查询；详细状态需support，不表示模型已验证 |
| GET /skills | GET /skills | 只读；必须带当前API令牌 |
| POST /skills/reload | POST /skills/reload | support热加载IT Skills |
| POST /chat | POST /runs、/runs/{id}/reply、GET /runs/{id}/turns/{request_key}、/events | 改为异步任务，旧同步响应退役；必须给每轮request_key，网络重试复用原键，不以新POST实现轮询；按原轮次读取回执与SSE |
| GET /monitor | GET /monitor | 主执行/专家/工具分别统计；普通用户限本人观测，support限同租户 |
| GET /trace/tool/{request_id} | GET /trace/tool/{run_id} | 一个会话可有多轮；固定turn_key，按after/through分页，默认200条并显式给next_after |
| GET /trace/tools | GET /trace/tools | 当前身份最近轮次；before/next_before游标，不开放其他人的对话 |
| GET /metrics | GET /metrics | support鉴权Prometheus；累计Counter/Histogram及兼容Gauge |
| POST /search | POST /knowledge/search | 旧query参数接口退役；JSON含query/top_k/mode；显式vector故障不冒充词法成功 |
| POST /knowledge/add | POST /knowledge/add（或/batch） | 保留documents数组包裹；support、最多100篇、JSON请求10 MiB；整批校验及SQL原子提交，返回逐篇结果/added_chunks/total_chunks/atomic_sql |
| POST /knowledge/upload | POST /knowledge/upload | 服务端multipart，file字段；UTF-8 .txt/.md/.json，文件10 MiB；JSON为最多100篇数组。与原忽略非法字节不同，非法UTF-8明确422；不再由浏览器解析并逐篇提交 |
| GET /knowledge/stats | GET /knowledge/stats | 只统计该身份有权查看的片段 |
| POST /eval/run | POST /eval/run | 持久任务；IT融合意图/检索/当前完整业务评测、自定义数据、逐轮Judge/断言、总时限；同账本计费，原电商标签不直接适用于IT域 |
| GET /eval/jobs/{job_id} | GET /eval/jobs/{job_id}、/resume、/baseline | 身份限定、持久回执、显式续时/恢复及基线选择；清理后410，不自动重做未知付费步骤 |
| GET /integrations/embedding | GET /integrations/embedding（与/intent-embedding同处理器） | support读取实际意图provider/冷却状态；知识向量另见/system，不混为一项 |
| GET /integrations/mcp | GET /integrations/mcp | 配置连接状态与protocol_endpoint；GET /mcp保留状态别名 |
| POST /integrations/mcp/call | POST /integrations/mcp/call | support、只读角色白名单、参数上限及协议验证；不调用模型，不绕过业务确认 |

标准MCP使用 `/mcp/knowledge/`，支持现有Bearer身份的Tools/计数/引用Resource，和状态接口分开。原 `/mcp` 挂载位置改变是显式协议URL变化。

## 导入的一致性与限制

- 三个导入入口共用每worker两个槽位；连接取消后底层线程未结束时仍保留槽位，应用关闭等待已开始的导入。不保证客户端断开就回滚；断开后先查知识状态再重试。
- 缺少document_id时依据租户、私有所有者、标题与内容产生稳定ID，相同内容重试不增加重复文档。需要替换原文时传入原document_id；改内容且省略ID会新建。
- 拒绝同批重复ID、空白字段、未知身份字段；任何校验、嵌入计算或私有所有权冲突导致整批SQL不写入。锁按文档ID排序，避免并发批次反向锁顺序。
- SQL文档、片段和Chroma outbox同事务，索引在提交后投影；故障返回index_pending，旧索引仍需SQL权限/版本复核，不能表示“所有存储跨系统原子提交”。实际PG/Chroma/Redis回归覆盖批量投影、私有可见性和缓存更新。
- 文件本体最多10 MiB，multipart另给64 KiB封装空间；网关允许11 MiB总请求，服务端仍强制文件边界。JSON请求本身最多10 MiB。当前本地E5按8条分批，较大文件耗时取决于CPU，未做10 MiB全量E5吞吐承诺。

## 交互CLI

先启动正常服务，在项目根运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m resolvedesk.cli --base-url http://127.0.0.1:8080 --actor learner
```

容器入口改为 `--base-url http://127.0.0.1:18080`；support身份用 `--actor support`。CLI从本地ignored的`.codex-runtime/access.json`读取API令牌，也可由`RESOLVEDESK_API_TOKEN`环境提供。不是模型Key，不放在命令行参数；远程地址必须HTTPS。CLI通过同一HTTP主入口使用同一身份、记忆、RAG、专业节点与MCP，不另建CLI专用Agent/数据库。

输入`:help`查看命令；支持`:runs`、`:use RUN_ID`、`:new`、`:memory`、`:search 查询`、`:integrations`、`:recent`、`:trace`、`:pause`、`:resume`和`:quit`。选择已有本人会话后直接输入会继续对话；暂停/失败需显式恢复。

聊天网络结果不确定时只能先`:retry`，复用原请求键和原轮次回执；恢复接口没有独立请求键，丢失恢复响应时只查询状态，不自动再次执行。草稿必须以`:confirm VERSION`核对所显示版本，不能一句自然语言确认就直接建单；确认仍走主服务精确审批契约。业务数据明确为模拟，不接真实企业审批。

免费部署检查：`python -X utf8 -B tools/verify_api_deployment.py http://127.0.0.1:18080`。它查询17项替代路由、实际上传拒绝边界和真实CLI进程，不写入知识、不调用模型；成功导入及事务故障使用隔离测试库验收。

## 最终验收

发布版本`a73e7c1`：334项Python、14子测试通过；前端29项与构建通过。两入口API/真实CLI各11/11，分别见`reports/api-cli-deployment-20261027T111731015990Z.json`、`reports/api-cli-deployment-20261027T111731083923Z.json`。同时两入口检索各60/60、标准MCP各11/11；部署验收预算保持2.441194元、579次已结算、未决0。

回归中实际发现并修复了“批量前一篇投影成功仍因后一篇未完成而误报index_pending”的问题：整批投影后按每篇当前outbox状态返回，不再借用全库pending计数。真实PG/Chroma/Redis及完整回归已通过。生产环境吞吐和大文件全量E5耗时不在本次证明范围。
