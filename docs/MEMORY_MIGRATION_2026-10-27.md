# G05 记忆与共享费用账本验收

日期：2026-10-27。范围固定为审计M02–M06/G05，不将本包完成等同于全部G01–G13完成。

## 当前实际链路

`resolvedesk.app`构造共享Redis协调器 → `DeskServices`接入`resolvedesk.memory.ScopedMemory` → 前台`context()`只读现有摘要、画像、近期消息和语义历史，提交维护水位 → 固定后台池维护 → 原始消息仍在SQL中，Chroma仅作私有派生索引。`DeskAgent`对追问、确认草稿和完成等终态均入队；关闭时先停止维护与MCP，再关闭共享模型客户端。

| 验收要求 | 实现与证据 |
| --- | --- |
| 增量压缩、合并旧摘要、原文可追溯 | `memory_summary.upto`只推进新旧消息段，保留完整messages；前台对未压缩的旧消息提供带seq摘录，最近6条工作消息保留。模型失败转确定性摘录，记录degraded和错误类型 |
| 多段情景、同会话优先后跨会话 | 每4条消息形成稳定slot，尾段可替换当前指针，旧分段不冒充当前版本；独立私有Chroma集合分别按当前run/其他run查询，metadata精确过滤本人/租户/私有，再验证SQL当前指针、模型和版本 |
| 丰富画像 | 环境、偏好、产品和问题类型；新模型事实必须引用用户消息seq及连续原文，value必须在quote中；助手内容、虚构原文、密码/令牌不允许写入；跨会话按原消息seq防旧事实覆盖新纠错 |
| 有界后台维护 | 每进程2个worker、每批32条消息；按用户合并所有会话，每租户128个活跃用户、全局512个。数据库锁串行容量检查；满队列保留messages，后台发现缺失水位后补排，不丢原文 |
| 维护互斥与恢复 | PostgreSQL认领令牌与90秒数据库时间租约，提交前再次核验；进程崩溃/陈旧结果不能覆盖新认领者。付费意图游标在网络前提交，未知旧尝试不会自动重发收费 |
| 工作缓存与索引失败 | Redis缓存键包含租户/本人/run/消息序号/记忆修订/摘要与索引水位，TTL1800秒；索引outbox与SQL记忆同事务，失败后可恢复。正式PG服务不会退回SQL线性向量扫描 |
| 前台故障隔离 | 前台不调用压缩模型；Redis/语义召回失败返回明确degraded，保留摘要/工作消息和原文；维护失败不把已经完成的聊天改为失败。超时embedding线程实际结束前不释放名额，避免无限创建后台线程 |

旧`memory_episode`、`memory_profile`及旧摘要保留。后台依据原messages重建新分段，不虚构旧画像的来源，也不丢弃旧行。显式SQLite离线fixture保留精确余弦测试路径，正式PG路径要求Chroma。

## 动态验收

- `tests/test_memory_maintenance.py`：来源验证、纠错顺序、原文保留、分段尾部更新、按用户合并/容量/补排、崩溃付费水位、陈旧令牌拦截、真实PG竞争、真实私有Chroma/Redis及故障恢复、超时线程名额、跨文本边界脱敏。
- 全部Python回归259项通过，另14个子测试通过；包含真实PostgreSQL/Redis/Chroma集成。前端本轮没有改动，镜像内沿用已验证构建。
- `reports/memory-runtime-20261027T070338442792Z.json`：真实Flash、本地固定E5、PG/Redis/Chroma/stdio MCP，隔离的当前ASGI入口14/14。证明丰富画像、压缩摘要、4段长对话档案、私有召回、真实Redis命中、新主请求实际注入并完成；未建业务工单。最后的脱敏和线程边界加固另由上述回归覆盖。
- `reports/services-runtime-20261027T070512764287Z.json`和`...070515408287Z.json`：容器和宿主机分别60/60免费检索，费用未变化。
- 正式库后台17个会话维护状态ready，索引outbox17个done；无活跃遗留维护任务。

## 费用切换与恢复边界

合成账本的跨Windows/Linux共享SQLite试验失败，报告保留为`reports/ledger-concurrency-20261027T065344950029Z.json`。真实旧账本未参与，切换前integrity_check=ok、339条settled、累计1.599820元。

旧应用停止后建立`.codex-runtime/checkpoints/finance-20261027T070151919319Z-05fa8b58/`：包含PG完整dump和原费用SQLite一致性快照。339条原记录内容SHA256为`811b8ddbe806ff757258c588aa9d0aa3e9e6ddf34b0decff4d099ee2d7fd193d`，新PG源逐条摘要一致。导入事务已经完成后，初次报告因PG SUM返回Decimal而序列化失败；修正JSON类型后重新核验全部记录，没有重导入、清空或回退费用。证据为`reports/financial-migration-20261027T070218370030Z.json`。

所有`BudgetedAnthropic`默认连接同一个`resolvedesk_finance.paid_calls`。预留与结算使用事务advisory lock；源缺失或未显式迁移时阻断调用，不静默新建费用源。`reports/financial-runtime-20261027T070342146002Z.json`在独立合成PG schema中验证真实宿主机/容器并发，共90条settled、无丢行或错误、严格18元操作上限，无真实模型消费。

切换后真实调用继续追加PG，不再写原SQLite。最近查询累计1.675506元、364条settled、未知预留0，保留2元余量后可用16.324494元；它是该时刻快照，后续以当前PG为准。备份早于后续新增消费，不能恢复费用schema来重置累计预算。恢复应用数据时必须单独保留最新费用记录；Git不能恢复数据库。原SQLite、旧记忆表和全部快照均保留，未上传。

## 后续固定范围

G06工具缓存/超时/熔断/知识故障回退、G07配置化MCP与服务端、G08完整评测、G09监控、G10轨迹、G11导入与CLI及G12/G13已列剩余项继续推进。此处没有宣称整体技术深度已全面超过原版。
