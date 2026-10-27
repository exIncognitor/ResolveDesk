"""Validated current-engine evaluation data, including per-turn assertions."""
from typing import Literal

from pydantic import AliasChoices,BaseModel,ConfigDict,Field,field_validator,model_validator

from resolvedesk.intent import CATALOG,DOMAIN_CATEGORY


class EvalInput(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,populate_by_name=True)


class HistoryMessage(EvalInput):
    role:Literal['user','assistant']
    content:str=Field(min_length=1,max_length=4000)


def intent_label(value):
    if value is not None and value not in CATALOG and value not in DOMAIN_CATEGORY:
        raise ValueError('Expected intent must be a current IT domain or fine intent')
    return value


class IntentCase(EvalInput):
    id:str=Field(default='',max_length=100)
    message:str=Field(min_length=1,max_length=4000)
    expected_intent:str=Field(validation_alias=AliasChoices('expected_intent','expected'))
    history:list[HistoryMessage]=Field(default_factory=list,max_length=50)
    context:dict=Field(default_factory=dict)
    _intent=field_validator('expected_intent')(intent_label)


class Assertions(EvalInput):
    expected_intent:str|None=None
    expected_agent:Literal['general','technical','billing','escalation']|None=Field(default=None,validation_alias=AliasChoices('expected_agent','expected_role'))
    expected_status:Literal['completed','awaiting_input','awaiting_confirmation','failed','paused']|None=None
    required_tools:list[str]=Field(default_factory=list,max_length=20)
    expected_entities:dict[str,list[str]]=Field(default_factory=dict)
    answer_contains:list[str]=Field(default_factory=list,max_length=20)
    answer_excludes:list[str]=Field(default_factory=list,max_length=20)
    _intent=field_validator('expected_intent')(intent_label)

    @field_validator('required_tools')
    @classmethod
    def tools(cls,values):
        import re
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',v) for v in values):raise ValueError('Invalid required tool')
        return values

    @field_validator('expected_entities')
    @classmethod
    def entities(cls,value):
        if len(value)>32 or any(len(k)>80 or len(values)>20 or any(len(v)>200 for v in values) for k,values in value.items()):raise ValueError('Expected entities too large')
        return value


class DialogTurn(Assertions):
    message:str=Field(min_length=1,max_length=4000)
    reference:str=Field(default='',max_length=12000)
    evidence:str=Field(default='',max_length=20000)


class DialogCase(Assertions):
    id:str=Field(default='',max_length=100)
    question:str|None=Field(default=None,min_length=1,max_length=4000)
    turns:list[DialogTurn]=Field(default_factory=list,max_length=50)
    reference_answer:str=Field(default='',max_length=12000,validation_alias=AliasChoices('reference_answer','reference'))
    evidence:str=Field(default='',max_length=20000)
    documents:list[str]=Field(default_factory=list,max_length=30)
    user_id:str=Field(default='learner',min_length=1,max_length=100)
    conv_id:str|None=Field(default=None,min_length=1,max_length=100)
    workflow:Literal['access_request','device_repair']|None=None
    expected_fields:dict=Field(default_factory=dict)

    @field_validator('turns',mode='before')
    @classmethod
    def string_turns(cls,values):
        return [{'message':v} if isinstance(v,str) else v for v in values] if isinstance(values,list) else values

    @model_validator(mode='after')
    def usable_turns(self):
        if self.question and self.turns:raise ValueError('Provide question or turns, not both')
        if not self.question and not self.turns:raise ValueError('At least one dialog turn required')
        if self.question:self.turns=[DialogTurn(message=self.question)];self.question=None
        if self.user_id=='support':raise ValueError('support is reserved for the sandbox operator')
        if not set(self.expected_fields)<={'summary','description','category','priority','workflow','resource_id','access_level','duration_days','asset_id'}:raise ValueError('Unknown expected draft field')
        return self


class ResumeEvaluation(EvalInput):
    extra_seconds:int=Field(default=0,ge=0,le=3600)


class AssetFixture(EvalInput):
    id:str=Field(pattern=r'^LT-[0-9]{3,8}$')
    owner:str=Field(default='learner',min_length=1,max_length=100)
    model:str=Field(default='合成评测设备',min_length=1,max_length=200)
    warranty:Literal['in_warranty','expired']='in_warranty'


class EvalDataset(EvalInput):
    title:str=Field(default='自定义评测集',min_length=1,max_length=200)
    provenance:str=Field(default='用户提供；来源未由服务端独立核实',max_length=2000)
    version:str=Field(default='custom-v1',min_length=1,max_length=100)
    intent_cases:list[IntentCase]=Field(default_factory=list,max_length=200)
    dialog_cases:list[DialogCase]=Field(default_factory=list,max_length=100)
    assets:list[AssetFixture]=Field(default_factory=list,max_length=100)

    @model_validator(mode='after')
    def nonempty_and_identified(self):
        if not self.intent_cases and not self.dialog_cases:raise ValueError('Empty evaluation dataset')
        from resolvedesk.store import encode
        if len(encode(self.model_dump()).encode())>1_000_000:raise ValueError('Evaluation dataset exceeds 1MB')
        for label,cases in (('intent',self.intent_cases),('dialog',self.dialog_cases)):
            for index,case in enumerate(cases):
                if not case.id:case.id=f'{label}-{index+1}'
            if len({case.id for case in cases})!=len(cases):raise ValueError('Duplicate case IDs')
        if len({asset.id for asset in self.assets})!=len(self.assets):raise ValueError('Duplicate asset fixtures')
        return self


class EvaluationOptions(EvalInput):
    kind:Literal['retrieval','quality']='retrieval'
    limit:int=Field(default=6,ge=1,le=100)
    use_judge:bool=True
    suite_id:str|None=Field(default=None,min_length=1,max_length=100)
    intent_cases:list[IntentCase]|None=Field(default=None,max_length=200)
    dialog_cases:list[DialogCase]|None=Field(default=None,max_length=100)
    assets:list[AssetFixture]=Field(default_factory=list,max_length=100)
    timeout_s:int=Field(default=540,ge=1,le=3600)

    @model_validator(mode='after')
    def selection(self):
        if self.suite_id and (self.intent_cases is not None or self.dialog_cases is not None):raise ValueError('Select a saved suite or inline cases')
        if self.kind!='quality' and (self.suite_id or self.intent_cases is not None or self.dialog_cases is not None):raise ValueError('Custom intent/dialog data requires quality evaluation')
        return self
