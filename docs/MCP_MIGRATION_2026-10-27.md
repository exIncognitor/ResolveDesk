# G07 当前 MCP 全链路与验收

日期：2026-10-27。固定范围对应审计C01–C06/G07，使用已固定的官方MCP SDK 1.26.0。整体迁移继续以G01–G13清单为准，本包完成不等于全部完成。

## 实际入口

| 地址 | 用途与身份 |
| --- | --- |
| `GET /mcp` | 原状态JSON，现有API令牌鉴权；不是MCP协议 |
| `/mcp/knowledge/` | 标准Streamable HTTP MCP协议，复用现有API Bearer身份；只读，不调用付费模型 |
| `GET /integrations/mcp` | 当前服务连接、协议版本、真实连接次数、已发现白名单工具和知识协议地址 |
| `POST /integrations/mcp/call` | support专用、Schema及角色白名单约束的只读调试；body为tool/arguments/role，不允许覆盖用户/租户，不调用模型 |
| `GET /integrations/intent-embedding` | support读取当前IT识别器实际provider、模板状态、失败类型、冷却剩余时间与尝试次数；观察现有状态，不发模型请求 |

知识协议导出`search_local_knowledge(query,top_k,mode)`、`resolvedesk://knowledge/stats`、`resolvedesk://knowledge/citations/{citation}`。每个HTTP请求重新鉴权，stateless模式不把初始化令牌身份锁在会话中。ContextVar只由认证包装器设置，工具参数不能指定身份。查询复用当前免费可靠检索；Resource直接查询同范围SQL，旧引用/无权限不返回原文；计数不包括其他用户私有数据。

## 正常启动配置

默认读取`deploy/mcp_servers.json`，包含本地只读support_checklist。自定义时使用`RESOLVEDESK_MCP_SERVERS_CONFIG`指向JSON，参考`deploy/mcp_servers.example.json`，`{python}`/`{project_root}`在服务端确定为当前解释器与项目根，不经shell扩展。路径相对项目根。最多16个服务，工具必须是明确字符串列表，角色仅允许已定义IT角色；`read_only`必须为布尔，当前入口拒绝false。

HTTP使用`transport=streamable_http`、`url`和`headers_env`。JSON只存头名到环境变量名的映射，专用变量值在本机.env或部署环境设置；stdio仅传递明确env_vars和SDK基础环境。显式模型Key变量禁止传给MCP，HTTP不跟随重定向、不读取环境代理。带凭据URL、query/fragment不接受。兼容`MCP_SERVERS_CONFIG`仅在`MCP_EXTERNAL_ENABLED=true`且没有新配置名时生效。

远端身份是配置的专用服务身份，不自动冒充当前聊天用户，也不转发其Authorization。应使用最小只读权限；不能把高权限support令牌作为所有用户的远端访问身份。只读标记/白名单是客户端控制，不是对不受信任远端实现的权限沙箱；stdio只运行管理员明确信任的本地程序。

连接使用标准SDK初始化/工具发现，目录通知刷新并使旧缓存失效。默认每15秒标准ping发现空闲断线，随后指数退避重连；每次注册重新接到专业节点。实际连接使用的凭据快照和当前凭据均从返回值/键中清除，防环境变量轮换或远端回显泄露。状态不输出头值、模型Key或连接密码。

## 保留的业务写入边界

C06的泛化写MCP明确不迁移：当前只允许配置read_only=true、工具annotations明确readOnlyHint=true且destructiveHint不是true，并匹配工具/角色白名单。调试接口不能绕过这些检查。权限、维修等写入仍通过本地草稿确认/审批/事务，不能通过远端工具自动执行。该边界是固定范围允许的明确选择，不是未完成项。

## 证据

- 全量Python回归276项/14子测试通过；传输归因和严格配置后相关14项再次通过。`tests/test_current_mcp.py`经当前App生命周期和标准客户端验证鉴权、并发身份隔离、同一会话切换令牌、租户/私有数据、资源计数、引用替换、正常配置HTTP、身份头/凭据快照脱敏、角色/写工具阻止、目录通知、实际停服重启后的被动重连、缺凭据和向量冷却状态。原stdio集成继续保留。
- `reports/mcp-runtime-20261027T081017528404Z.json`：真实PG/Redis/Chroma/固定E5/stdio/鉴权HTTP，当前ASGI与真实Flash 17/17。免费协议和调试没有费用；真正由主入口的专业节点选择并调用配置HTTP工具，轨迹具有实际server/tool/source；主任务完成且有引用、无业务写入，凭据未进入持久事件。
- 外部HTTP服务使用隔离的本机标准服务验证，不冒称已连接企业第三方系统。正式默认保留本地stdio清单；使用真实外部系统需要配置该系统的地址和专用凭据。
- 验收数据为合成，独立SQL schema/Chroma collection/Redis namespace和配置文件由验证器核对归属后清理，原业务、账本、模型和容器卷保留。

本次真实验收累计费用从1.724282元增至1.759536元，388条settled，未知预留0；只追加原PG权威费用源，继续保留2元安全余量。最新值以当前账本为准。没有专用重排模型费用。

## 使用标准客户端

以网关为例，MCP URL为`http://127.0.0.1:18080/mcp/knowledge/`，HTTP头为`Authorization: Bearer <本地API访问令牌>`，使用与网页相同的learner/support身份。令牌从本机ignored的`.codex-runtime/access.json`取得，不是模型API Key。部署到自定义域名时，明确配置`RESOLVEDESK_MCP_ALLOWED_HOSTS`和`RESOLVEDESK_MCP_ALLOWED_ORIGINS`；默认启用DNS rebinding防护，不使用任意Host/Origin放行。

免费部署验收：`python -X utf8 -B tools/verify_mcp_deployment.py http://127.0.0.1:18080`。隔离的真实模型专业节点验收需显式`tools/verify_mcp_runtime.py --live`，从同一预算付费，不应为看状态重复运行。

## 部署复核与恢复点

代码`d872793`最终再次通过276项/14子测试；宿主机两个worker和Docker应用均已运行，容器healthy。宿主机/网关标准SDK验收各11/11，证据为`reports/mcp-deployment-20261027T082010925740Z.json`和`...082010865652Z.json`；免费检索各60/60，证据为`reports/services-runtime-20261027T082012746935Z.json`和`...082012690898Z.json`。均未增加费用。

`reports/mcp-release-20261027T082208690409Z.json`核对运行镜像标签和6个关键源码/配置SHA256，与已测工作区一致。代码由Git保留；部署前PG完整备份位于`.codex-runtime/checkpoints/g07-20261027T081652326906Z-be48c5e6/`，未上传。恢复应用数据时必须单独保留最新费用记录，不能用旧快照重置预算；没有清除容器卷或旧数据。
