# 有来源知识的发布管理

普通检索仍使用 PostgreSQL 授权、BM25、Chroma HNSW 和 RRF。`knowledge_publications` 是独立 SQL 发布表，不修改旧 `knowledge_documents` 的七列契约。已有无元数据文档保持原读取兼容；新的受管理文档带 `metadata` 导入后为草稿。

JSON 导入在原 `/knowledge/add`、`/knowledge/upload` 接口增加可选 `metadata`。字段示例见 `knowledge/reference/catalog.json`；`path` 是本地目录字段，上传前应移除并填入 `content`。材料分为 `technical_reference` 和 `simulated_policy`，必须填写适用范围、现有业务域、来源核对日期、生效/到期秒数。技术参考要求 HTTPS 来源，地址仅作为引用，不由服务自动抓取。请勿在 URL 中放凭据。

客服接口：

- `GET /knowledge/publications`：查看本租户公开或本人私有的受管理材料，返回内容 `version`、发布元数据 `revision`、状态与 `readable`。
- `POST /knowledge/publications/{document_id}/review`：发送 `action=publish|withdraw`、`version`、`revision` 和非空 `note`。内容或元数据已变化时返回 409；过期材料不能直接发布。
- `PUT /knowledge/publications/{document_id}/metadata`：发送当前两个版本、变更说明与完整 `metadata`；修改后回到草稿。
- `GET /knowledge/publications/{document_id}/audit`：查看来源审核、发布与撤回的操作者和版本记录。

Vue 知识页提供对应操作。审核权限由当前令牌的客服角色决定，私有材料仍只能由其所有者管理；Agent、普通用户和读取型 MCP 没有发布权限。当前系统没有独立审核人双人签署机制，审核人记录实际客服身份；本次初始语料为助手在用户授权下核对来源，不能声称由真实企业专家独立审核。

内容更新、元数据更新均会使已发布内容回到草稿。审核/撤回增加文档代数并写审计，现有 outbox 投影最新 SQL 状态。未来生效和到期各有定时投影任务；SQL 当前发布状态与数据库时钟在索引延迟时继续起最终过滤作用。Redis、本进程缓存、引用接口、统计与评测快照都使用同一规则。旧暂停会话检测到来源元数据变化后重新取得上下文，已保存的历史轮次和确认契约保留。

`evidence_sufficient` 是检索信号，不保证答案正确，更不代表技术参考能回答企业具体制度。阈值由独立开发集确定；冻结验收集中保留了 8 个正例保守追问。受管理知识没有足够依据时，咨询流程先询问产品/版本/错误信息；已明确的业务申请仍可收集草稿，写入行为继续受确认、审批、字面字段与模拟业务规则约束。

首次本地发布工具：`python tools/publish_reference_knowledge.py` 只预览，`--apply` 才导入并审核。它先检查公共运行任务并生成验证过的全库备份，再导入固定 26 篇参考；对 8 篇原始示例核对 SQL 正文后仅附加元数据，保留旧版哈希与片段 ID。它不覆盖正文不同的用户资料，也不触碰费用账本。
