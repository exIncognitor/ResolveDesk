# Test-NetConnection核验TCP连接

对获准的主机与端口使用Test-NetConnection查看TCP连通性及相关诊断。TCP端口结果不能直接代表同端口UDP服务，也不能证明业务登录成功。记录目标、端口、时间与网络环境，不扫描未经授权的地址段。

资料性质：技术参考，具体操作须匹配产品、版本及企业授权。

官方来源：[Windows PowerShell NetTCPIP](https://learn.microsoft.com/en-us/powershell/module/nettcpip/test-netconnection?view=windowsserver2025-ps)
核对日期：2026-10-27。本文为项目自写摘要，不能代替厂商完整文档或企业制度。

