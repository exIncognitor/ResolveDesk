# 第三轮交付与验收

目标为 `ITERATION3_GOAL.md`，实现和质量结果见 `ITERATION3_PROGRESS.md`，操作接口见 `KNOWLEDGE_PUBLICATION.md`。范围限定为多轮实体稳定性与知识深度/发布管理，没有新增业务领域或基础设施。

## 实际运行

- 后端运行代码检查点：`f93c1ff`。Docker 镜像带同一 revision 标签，宿主机 8080 运行两个 Python worker，Docker 入口为 18080。代码目录逐文件 SHA-256、依赖版本、前端 JS/CSS 均与本地交付物一致。
- 已发布 26 篇有官方来源的技术参考和 8 篇原始模拟制度；原始文档正文、版本、片段 ID 不变。初始审核实际由助手在用户授权下完成，审计中明确说明。
- 最新部署报告：`reports/iteration3-deployment-20261027T160638527015Z.json`，30/30。两入口分别通过 API/CLI 11/11 与标准 SDK MCP 11/11；对应报告在 `reports/api-cli-deployment-*`、`reports/mcp-deployment-*`。这些检查不调用付费模型。
- 浏览器已核对知识发布入口的移动布局与未登录提示；带身份的数据、来源、审核权限和检索验证来自真实认证 API。没有把未登录浏览器视图冒充完整人工审核测试。

## 回归与发布检查

整合阶段完整真实服务回归 412 项、14 子测试通过；随后新增旧暂停会话发布元数据刷新回归，包含该调整的定向 52 项通过。最终完整本地报告保存为 `.codex-runtime/iteration3-final-tests.xml`，GitHub 当前交付提交必须通过四项工作流：Windows Python、Linux Python、Vue、PostgreSQL/Redis/Chroma 集成。最终状态以该提交的 GitHub Actions 记录和 `.codex-runtime/iteration3-ci-final.json` 为准，没有 success 记录时不能描述为通过。

Vue 32 项测试通过并构建。发布内容扫描无凭据或本地资料，`pip check` 通过。依赖审计为 `pass_with_profile_exclusions`：Chroma 1.5.9 的四项公告在已核验的 Rust 服务、内部入口、显式向量和主路径不使用 Python SDK 的部署画像下有期限排除，2026-11-26 到期。这不是上游修复，也不是零已知漏洞。未启用外部 Webhook 或发送通知。

质量对比摘要为 `reports/iteration3-comparison-summary.json`；所有原始报告仍在。字面实体 27/80→80/80、状态序列 2/12→12/12；真实 Flash 两轮从 3/8、3/8 改善到 5/8、6/8，仍有首轮状态/工具契约失败，不能声称全部真实对话通过。同 26 篇语料检索 Recall@5 均为 100%，MRR@5 均为 0.957265，没有召回提升的结论。候选判充分依据 70/78，另 8 题保守追问，无依据题误报 0/2。数据为合成小样本。

付费真实对比固定使用实体阶段 `d402425` 副本；发布治理后的最终代码未追加同规模真实模型复测。最终代码的新增逻辑由离线/真实依赖回归验证，不能混用代码身份。累计账本为 8.864576 元、1931 次 settled、无未决预留；本轮新增 2.718244 元，未超过 3 元上限，后续部署与验证费用未增加。总额 20 元保留 2 元后剩余 9.135424 元。

## 恢复位置

修改前版本以本地 Git 检查点留存；基线 `f09efe7f25fe8028f2df97ef39b989bfd97212b0`，运行版本 `f93c1ff`。未生成相邻 history 目录。固定基线与实体候选执行副本、受限清理的 pytest 目录和真实依赖评测 fixture 保留为证据，没有绕过此前清理限制。

发布前完整 PostgreSQL 备份：

`.codex-runtime/checkpoints/eval-retention-20261027T160055984116Z-f4428f32/resolvedesk.dump`

SHA-256：`f3fc0895c8ae5afbe7c90d7b1c4e01c9cae94367418e65d1ce5ce5bf9648fa6e`。已用 `pg_restore -l` 验证；含敏感本地数据，不提交 GitHub。此前预检查生成的另一份备份也保留。

恢复必须配套考虑应用数据与代码。旧检索代码不理解发布状态，不能只回退代码后继续使用新的发布数据。不得通过整库恢复、评测恢复或迁移重置 `resolvedesk_finance` 累计消费；只在明确恢复授权下选择所需应用 schema，保留恢复时的真实费用记录。本轮没有执行数据库恢复或删除。
