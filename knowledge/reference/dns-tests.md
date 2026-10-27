# DNS短名与完整域名对照测试

在批准的域名和DNS服务器范围内进行只读查询，对照短名与FQDN结果。完整域名可用但短名失败时检查DNS后缀配置。nslookup直接向配置的DNS服务发起查询，不等同于复用客户端缓存。

资料性质：技术参考，具体操作须匹配产品、版本及企业授权。

官方来源：[Windows DNS客户端/服务器诊断](https://learn.microsoft.com/en-us/windows-server/networking/dns/troubleshoot/troubleshoot-dns-client)
核对日期：2026-10-27。本文为项目自写摘要，不能代替厂商完整文档或企业制度。

