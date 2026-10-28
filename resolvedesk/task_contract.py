"""Literal request consent and server-owned workflow/fact-source decisions.

Intent fusion chooses expertise. It does not grant consent to create requests.
This small grammar records explicit request language and keeps it across omitted
slots; no model tool argument can change the resulting workflow.
"""
import re

WORKFLOWS={'access_request','device_repair','license_request'}
REQUEST=re.compile(r'申请|报修|准备|提交|创建|开通')
QUESTION=re.compile(r'(?:怎么|如何|怎样|是否|能否|可以|需要|要不要|能不能|什么条件).{0,6}(?:申请|报修|提交|开通)|(?:申请|报修).{0,8}(?:流程|条件|要求|政策|手续)')
CONSULT=re.compile(r'(?:仅|只)(?:是|想)?(?:咨询|了解|解释|查询)|(?:不(?:要|用|需)?|别|先别|先不)[^，。；！？,.!?;]{0,5}(?:准备|创建|提交|申请|报修)')


def is_cancellation(message):
    """Recognize an affirmative cancellation of the current request, not a negation.

    Possessives and demonstratives belong to the object, not a fixed list of
    entire utterances. Actual submitted cases still require the action API.
    """
    clause=re.split(r'[，。；！？,.!?;]',message.strip(),maxsplit=1)[0]
    return bool(re.fullmatch(
        r'(?:(?:请|麻烦)(?:帮我)?|帮我|我想|我要)?\s*(?:取消|撤销|放弃)'
        r'(?:(?:我|之前|原来|刚才|本次|这次|这个|这份|那个|全部|当前|的|权限|维修|软件|席位|访问|报修|商业授权)\s*)*'
        r'(?:申请|草稿|任务|问题|处理)(?:吧|了)?',clause)
        or re.fullmatch(r'(?:先不处理|之前的全部取消)(?:了|吧)?',clause))


def request_workflow(message,domain):
    candidates=[]
    if re.search(r'权限|仓库|repo-|docs-',message,re.I):candidates.append('access_request')
    if re.search(r'报修|维修|修理|硬件|LT-',message,re.I):candidates.append('device_repair')
    if re.search(r'授权|席位|许可|Ultimate',message,re.I):candidates.append('license_request')
    if len(candidates)==1:return candidates[0]
    if len(candidates)>1:return 'ambiguous'
    return None


def update(previous,message,domain,turn_seq,*,reset=False,cancelled=False):
    previous=previous or {}
    if cancelled:return {'workflow':None,'consent':False,'source':'explicit_cancellation','turn_seq':turn_seq}
    consult=bool(CONSULT.search(message)) or bool(QUESTION.search(message) and not re.search(r'请(?:帮我)?(?:准备|提交|创建)|我要|我想|我需要|帮我申请',message))
    if consult:
        # Keeping a typed, unsubmitted request is useful when the user says
        # "先别准备草稿"; it never grants permission to prepare on this turn.
        explicit=bool(re.search(r'我要|我想|我需要|帮我',message))
        workflow=(request_workflow(message,domain) if explicit else None) or (previous.get('workflow') if not reset else None)
        return {'workflow':workflow,'consent':False,'source':'explicit_consultation','turn_seq':turn_seq}
    if REQUEST.search(message):
        workflow=request_workflow(message,domain)
        if workflow:return {'workflow':workflow,'consent':True,'source':'explicit_user_request','turn_seq':turn_seq}
        if previous.get('workflow') and not reset:return previous|{'consent':True}
        return {'workflow':None,'consent':True,'source':'explicit_generic_request','turn_seq':turn_seq}
    if not reset and previous:return previous
    return {'workflow':None,'consent':False,'source':'no_request_consent','turn_seq':turn_seq}


def facts(routing,contract):
    fine=routing.get('fine_intent')
    if routing.get('urgency')=='CRITICAL' or routing.get('intent')=='escalation':return 'handoff'
    if fine=='greeting':return 'greeting'
    if fine in {'ticket_status','asset_lookup'}:return 'business'
    if contract.get('workflow') and contract.get('consent'):return 'intake'
    return 'knowledge'


def missing_slots(contract,entities):
    keys={'access_request':('resource_id','access_level','duration_days'),'device_repair':('asset_id',),
          'license_request':('software','duration_days')}.get(contract.get('workflow'),())
    names={'resource_id':'资源 ID','access_level':'权限级别','duration_days':'使用天数','asset_id':'本人资产 ID','software':'商业软件产品与版本'}
    missing=[names[key] for key in keys if len(entities.get(key,[]))!=1]
    return missing
