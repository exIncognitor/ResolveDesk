# 评测保留与显式回收

当前应用使用 PostgreSQL、Redis、Chroma。正常运行不会定时删除评测、作者数据集或业务资料。默认批量预览只列出本人创建超过30天的已完成/失败/暂停作业，最多20项；精确指定 `--job-id` 表示主动选择该作业，不受年龄筛选限制。运行中、排队中、已选基线以及归属不能核实的旧沙盒受保护。

## 运维入口

在项目目录使用：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B tools\retain_evaluations.py --tenant demo --owner support --days 30
```

也可预览一个确定的作业：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B tools\retain_evaluations.py --tenant demo --owner support --job-id '<job-id>'
```

确认预览的 `plans` 中仅包含要回收的作业后，使用对应的 `confirmation`：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B tools\retain_evaluations.py --tenant demo --owner support --job-id '<job-id>' --confirmation '<preview-digest>' --apply
```

当前 CLI 的 apply 只接受已配置的本机 Compose PostgreSQL；远程数据库需要其对应的已核验备份流程，不会改成其他数据库或跳过备份。

API `GET /eval/retention` 和评测中心提供免费预览；必须使用 support 身份，只能查看自己的作业。API 不提供绕开备份的删除接口。

## 执行顺序及恢复边界

1. 完整 `pg_dump -Fc` 保存到 `.codex-runtime/checkpoints/eval-retention-<UTC>-<id>/resolvedesk.dump`；检查退出状态、归档目录和包含的 source/sandbox schema，记录 SHA-256。apply 前再次核验文件、摘要及覆盖范围。
2. 数据库锁串行基线选择和作业状态变更。重新检查计划摘要、本人/租户、基线保护、活动状态；将作业标记 retiring。清理状态有独立所有者/数据库时间租约，失败后可根据同一计划重试。
3. 正式沙盒必须拥有随机 `eval_rd_<uuid>` schema 和与完整原快照匹配的 `eval_sandbox_meta` 标记。知识/私有记忆 collection 和 Redis namespace 必须与快照登记完全一致。发现活动会话、有效记忆维护租约或未知表时停止。
4. 先完成只读归属检查和 SQL 依赖检查；逐表 `DROP TABLE`，最后 `DROP SCHEMA`，均不使用 CASCADE。外部视图/FK等依赖会阻止操作，SQL事务回滚，远端数据尚未删除。
5. 仅删除登记的两个 Chroma collection 和精确 namespace 内的 Redis key。远端失败时 SQL 回滚，作业标记 retire_failed；已移除的远端对象在重试时允许不存在，不重新执行任何模型或业务步骤。
6. 成功后清空评测正文、逐步结果和大快照，保留 job/unit 标识及输入/结果摘要墓碑，标记 expired；作者保存的数据集、已选基线、源业务库和费用账本均保留。过期作业无法 resume 或设为基线。非过期作业容量随之释放。

备份包含共享金融数据，但恢复只能选择需要恢复的应用证据。**不得恢复旧金融快照覆盖较新的 `resolvedesk_finance.paid_calls`**。备份是回滚资料，不在自动清理范围。

缺少资源登记的旧沙盒和显式 SQLite 离线测试夹具会在预览中列为 protected。它们需要单独核实归属及文件备份后再处理；服务回收器不猜测文件路径或 Redis namespace，不删除历史目录或 pytest 临时目录。

## 范围及验证

当前容量为全局200、每租户50个非过期作业，每作者50个不可覆盖的评测集；两个活动评测。单元结果2MB、报告8MB、证据快照20MB。超限明确拒绝，不自动删除。轻量墓碑持续保留，以禁止误重放。

- `tests/test_eval_retention.py` 覆盖权限/本人、基线、活动任务、计划变动、备份摘要/覆盖、容量释放、作者数据保留、真实三服务回收、远端失败重试、外部依赖阻断且无 CASCADE。
- `tests/test_eval_crash.py` 使用两个独立 Python 进程和真实 PostgreSQL，强杀本测试创建的 worker 后恢复；已完成或结果不确定的模拟付费步骤均只执行一次。
- `tools/verify_eval_retention_deployment.py --apply-synthetic` 只创建并回收本次新建的免费检索评测，核验当前 API/CLI、备份、墓碑、拒绝恢复和预算不变。不会批量清理既有结果。
