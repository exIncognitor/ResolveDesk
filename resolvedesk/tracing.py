"""Bounded secret redaction shared by persisted execution/tool traces."""
import os
import re


def redact_text(value,limit=1200):
    # Match secrets on the whole text before truncation. Splitting text before
    # redaction can expose a credential crossing a chunk boundary.
    for name in ("ANTHROPIC_API_KEY","DEEPSEEK_API_KEY","SILICONFLOW_API_KEY"):
        secret=os.getenv(name,"")
        if len(secret)>=8:value=value.replace(secret,"[REDACTED]")
    value=re.sub(r"(?i)\b(?:sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{16,}|bearer\s+[a-z0-9_.~-]+)","[REDACTED]",value)
    value=re.sub(r"(?i)((?:password|token|api.?key|secret|密码|验证码)\s*(?:=|:|：|是)\s*)[^\s,，;；]+",r"\1[REDACTED]",value)
    return value[:limit]


def redact(value,depth=0):
    if depth>5:return "[depth limited]"
    if isinstance(value,dict):
        return {str(k)[:80]:("[REDACTED]" if re.search(r"(?i)password|passwd|secret|api.?key|authorization|access.?token|refresh.?token|密码|验证码",str(k)) else redact(v,depth+1)) for k,v in list(value.items())[:40]}
    if isinstance(value,(list,tuple)):return [redact(item,depth+1) for item in value[:40]]
    if isinstance(value,str):
        return redact_text(value)
    if value is None or type(value) in {bool,int,float}:return value
    return type(value).__name__
