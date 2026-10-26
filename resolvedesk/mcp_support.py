"""ResolveDesk read-only support SOP service over standard MCP stdio."""
from typing import Literal
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("ResolveDesk Support")


@server.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False))
def support_checklist(category: Literal["network","access","software","hardware","other"]) -> dict:
    """Read a diagnostic intake checklist; no credentials, network or business writes."""
    fields = {"network":["错误码","发生时间","普通网络是否正常","已尝试的网络切换"],
              "access":["目标系统","错误提示","申请用途","期望权限级别"],
              "software":["软件版本","操作系统","错误摘要","最近变更"],
              "hardware":["设备类型","故障现象","影响范围"],"other":["问题现象","发生时间","影响范围"]}
    return {"category":category,"required_context":fields[category],"boundary":"不索要密码或验证码；不自动开通权限；工单需要用户确认。"}


if __name__ == "__main__":
    server.run(transport="stdio")
