"""Bounded secret redaction shared by persisted execution/tool traces."""
import json
import os
import re


def redact_text(value,limit=1200):
    # Match secrets on the whole text before truncation. Splitting text before
    # redaction can expose a credential crossing a chunk boundary.
    for name in ("ANTHROPIC_API_KEY","DEEPSEEK_API_KEY","SILICONFLOW_API_KEY","RESOLVEDESK_EMBEDDING_TOKEN","RESOLVEDESK_ALERT_WEBHOOK"):
        secret=os.getenv(name,"")
        if len(secret)>=8:value=value.replace(secret,"[REDACTED]")
    value=re.sub(r"(?i)\b(?:sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{16,}|bearer\s+[a-z0-9_.~-]+)","[REDACTED]",value)
    value=re.sub(r"(?i)(https?://|rediss?://|postgresql(?:\+psycopg)?://)[^\s/@]+:[^\s/@]+@",r"\1[REDACTED]@",value)
    value=re.sub(r"(?i)([?&](?:token|key|secret|password|signature|sign)=)[^\s&#]+",r"\1[REDACTED]",value)
    value=re.sub(r"(?i)\bBasic\s+[A-Za-z0-9+/]+=*","Basic [REDACTED]",value)
    value=re.sub(r"(?i)((?:password|token|api.?key|secret|密码|验证码)\s*(?:=|:|：|是)\s*)[^\s,，;；]+",r"\1[REDACTED]",value)
    return value[:limit]


def redact(value,depth=0):
    if depth>5:return "[depth limited]"
    if isinstance(value,dict):
        return {redact_text(str(k),80):("[REDACTED]" if re.search(r"(?i)password|passwd|secret|api.?key|authorization|(?:^|_)token$|access.?token|refresh.?token|cookie|credential|approval|密码|验证码",str(k)) else redact(v,depth+1)) for k,v in list(value.items())[:40]}
    if isinstance(value,(list,tuple)):return [redact(item,depth+1) for item in value[:40]]
    if isinstance(value,str):
        return redact_text(value)
    if value is None or type(value) in {bool,int,float}:return value
    return type(value).__name__


def summary(value,limit=4000):
    """Bounded JSON summary; redact the full value before imposing byte limits."""
    clean=redact(value)
    text=json.dumps(clean,ensure_ascii=False,separators=(',',':'))
    if len(text)<=limit:return clean
    return {'preview':text[:limit],'truncated':True}


def tool_trace(name,args,result,*,trace_id,tool_use_id,layer,role,instance,seconds,success,error_code=None):
    data=result if isinstance(result,dict) else {}
    return {'trace_id':trace_id,'tool_use_id':tool_use_id,'tool_name':name,'name':name,'layer':layer,
        'agent_type':role,'instance':instance,'input':summary(args,2400),'output_summary':summary(result),
        'latency_ms':round(max(0,seconds)*1000,3),'success':bool(success),'result_success':data.get('success'),
        'cached':bool(data.get('cached')),'cache_layer':data.get('cache_layer'),
        'fallback':bool(data.get('degraded') or data.get('fallback')),'error_code':error_code,
        'source':data.get('source','local'),'mcp_server':data.get('mcp_server'),'mcp_tool':data.get('mcp_tool')}
