# 开发与验证

[返回首页](../README.md) · [架构与调用链](CURRENT_ARCHITECTURE.md)

后端入口为 `resolvedesk/app.py`，前端为 `frontend/src/App.vue`。正式依赖为 PostgreSQL、Redis、Chroma；SQLite 用于显式离线夹具。宿主机开发可只把依赖放进 Docker。

## 日常检查

激活虚拟环境后：

```powershell
python -X utf8 -B -m pytest -q
python -X utf8 -B tools/check_release.py
npm --prefix frontend test
npm --prefix frontend run build
```

默认测试关闭付费调用，使用受控模型、HTTP或工具结果。前端回归覆盖连接身份、逐轮重试、SSE解析、历史恢复和界面竞态；界面改动还需要核对桌面与移动布局。

## 真实服务集成

启动独立本机服务后，Windows PowerShell：

```powershell
$env:RESOLVEDESK_SERVICE_TESTS = "true"
python -X utf8 -B -m pytest tests -q
```

Linux/macOS：

```bash
RESOLVEDESK_SERVICE_TESTS=true python -X utf8 -B -m pytest tests -q
```

集成测试使用随机PG schema、Redis命名空间和Chroma集合。服务可用性可用 `tools/check_services.py` 检查。免费实时检索验收为 `tools/verify_service_runtime.py`。

## 真实模型与质量评测

质量评测从当前 `/eval/run` 进入，经隔离服务走同一应用链，支持自定义数据和逐轮断言。生成、Judge、后台记忆都经过同一费用网关。详情见 [评测实现](EVALUATION_MIGRATION_2026-10-27.md) 与 [收尾验收](FINAL_POLISH_ACCEPTANCE.md)。

保持输入、知识发布状态、模型和评分配置可追踪；比较引擎版本时保留原始低分和失败。目录中早期脚本与当前评测口径的关系见文档中心。

## 目录与边界

| 目录 | 责任 |
| --- | --- |
| resolvedesk/ | 当前应用、业务状态、知识治理、评测和服务接线 |
| agents/、core/、tooling/ | 角色执行、模型网关、意图、Skill和MCP复用组件 |
| frontend/ | Vue工作台、API适配和前端回归 |
| knowledge/、skills/ | 内置知识、来源目录与角色规范 |
| tests/、evaluation/ | 确定性回归、集成测试和固定评测输入 |
| docs/、reports/ | 当前指南、设计解释与版本化证据 |
| tools/、deploy/ | 初始化、验证、迁移与部署配置 |

业务状态由服务端校验和事务推进；前端展示与提示不能替代权限。新增外部写工具时，先明确审批、幂等、未知结果与补偿契约。

## 本地资料

`.env`、`key.txt`、`.codex-runtime/`、`data/`、模型、数据库与账本留在本机，不提交。项目修改前建立Git检查点，保护已有未提交工作；被忽略的数据需要独立恢复点。

本应用按明确页面与静态assets提供资源，不发布源码、本地配置或历史目录。现有部署只监听本机，公开部署需按自己的环境配置网络入口和身份管理。

[返回首页](../README.md)

