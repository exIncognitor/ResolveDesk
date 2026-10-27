# ResolveDesk 技术选型依据与执行边界

决策日期：2026-10-27。目标为完整关闭技术迁移审计 G01–G13，并通过同范围验证证明改进。文档记录选择，不等于功能已实现。

## ADR-001 服务化存储职责

正式运行路径采用 PostgreSQL + Redis + Chroma。不能因本机依赖不可用静默回落为 SQLite/进程字典/线性向量扫描。

| 组件 | 职责 | 选择依据与限制 |
| --- | --- | --- |
| PostgreSQL | 消息、回合回执、任务及认领代次、业务状态、审计、评测、通知/索引outbox | 事务、唯一约束、行锁、条件更新适合多worker业务一致性。原有SQLite的BEGIN IMMEDIATE会序列化写入；它可保留为离线测试与迁移来源，但不作为服务化模式的暗中替代。 |
| Redis | 会话租约、工作记忆、摘要热数据、回执与检索缓存、协调 | 保留原技术职责，缓存可以从数据库重建。租约不是唯一正确性依据，持久写入还须验证认领代次。 |
| Chroma | 知识与分段情景记忆的服务化向量索引 | 保留标准客户端/服务端、HNSW索引及metadata过滤；本地固定版本E5负责embedding，不增加付费embedding调用。SQL仍保留文档版本与访问控制依据，防止索引滞后造成越权或旧引用。 |

暂不改为另一套向量数据库：已有Chroma客户端、可验证的过滤机制和原版迁移要求足以支持当前选择；没有同样本/负载对照证据时，换名字不构成优化。独立重排模型比较仍按用户既有要求不做，生成与Judge只允许deepseek-flash。

官方依据：

- Redis分布式锁文档明确讨论有效期、进程暂停/故障边界与fencing token：https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/
- PostgreSQL行锁与并发事务：https://www.postgresql.org/docs/18/explicit-locking.html
- PostgreSQL应用层一致性：https://www.postgresql.org/docs/18/applevel-consistency.html
- Chroma存储、检索及metadata过滤：https://docs.trychroma.com/

这些资料证明机制存在，不证明本项目已获得性能提升。需对实际当前入口进行并发、失租、故障恢复、访问隔离和检索对照验证。

## ADR-002 一致性边界

1. 业务数据库是持久事实源；每轮request_key与输入摘要必须由唯一约束保护。
2. Redis租约配合数据库认领令牌/代次。提交检查点、消息、业务结果前拒绝旧认领者。
3. SQL事务不能与Redis/Chroma伪装成一个原子事务。用事务outbox进行幂等缓存更新、索引更新与补偿。
4. Chroma检索先执行租户/权限metadata过滤，再向SQL验证当前文档版本和授权。缓存键包含权限范围、知识版本和查询参数。
5. 不将旧SQLite文件直接覆盖或丢弃。迁移前一致性快照、目标库空库校验、逐表计数/内容摘要与序列校正通过后，才切换启动配置。

## ADR-003 开发与部署

Docker依赖服务与应用分开启动，便于宿主机断点调试；最终Compose须包含应用、前端构建、Redis、PostgreSQL、Chroma、Prometheus/网关等完整部署路径。所有外部端口默认仅绑定127.0.0.1，持久数据使用独立命名卷，禁止自动清卷。

版本使用经实际拉取和测试的固定tag/digest。配置凭据仅存本机ignored文件，不写入源码、报告、镜像或工具输出。依赖故障明确失败或报告受控降级，不修改技术主线来掩盖问题。

## ADR-004 完成条件

`MIGRATION_AUDIT_2026-10-27.md` 的61项能力与G01–G13继续作为固定范围。每个完成项必须链接当前入口、针对性测试、真实服务集成或明确的动态证据。旧模块单元测试通过不构成新入口已接入；新合成集高分不构成原版对照提升。
