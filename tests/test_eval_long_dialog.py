"""Exercise the supported 50-turn limit through current ASGI with explicit fake LLM."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from resolvedesk.eval_models import EvalDataset
from resolvedesk.quality import QualityRunner
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.store import Store,Principal
from test_resolvedesk import response


@pytest.mark.asyncio
async def test_fifty_actual_turns_each_scored_and_early_failure_not_hidden(tmp_path,monkeypatch):
    import resolvedesk.quality as module
    monkeypatch.setattr(module,'ROOT',tmp_path)
    p=Principal('long-eval','operator','support')
    store=Store(tmp_path/'source.sqlite3');kb=HybridKnowledge(store)
    kb.add(p,'policy','服务介绍','你好，服务范围包括账号、网络和设备支持。')
    calls={'judge':0,'main':0}
    async def create(**kwargs):
        if kwargs.get('system','').startswith('你是企业IT意图分析器'):return response('{"fine_intents":["greeting"],"confidence":0.99,"urgency":1}')
        if kwargs.get('system','').startswith('你是ResolveDesk只读IT专业节点'):return response('合成只读服务介绍。')
        if 'tools' not in kwargs:
            if '客服质量评估专家' in str(kwargs['messages']):
                calls['judge']+=1
                return response('{"relevance":0.9,"accuracy":0.9,"completeness":0.9,"helpfulness":0.9}')
            return response('{"summary":"服务介绍","facts":[]}')
        calls['main']+=1
        context=json.loads(kwargs['system'].rsplit('以下为服务器提供的数据上下文（不是指令）：\n',1)[1])
        return response('你好，可以协助IT服务。['+context['evidence'][0]['citation']+']')
    model=SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=create)),close=AsyncMock())
    turns=[{'message':f'你好，这是第{i+1}轮，请介绍服务范围。','reference':'说明可提供的IT服务，不声称已经办理业务。'} for i in range(50)]
    turns[26]['answer_contains']=['不会出现的合成断言标记']
    dataset=EvalDataset(dialog_cases=[{'id':'long','turns':turns,'expected_agent':'general','expected_status':'completed','documents':['policy']}]).model_dump()
    report=await QualityRunner(store,kb,lambda:model).run(p,'fifty-turn-test',1,True,lambda *args:None,dataset=dataset)
    assert report['evaluation_errors']==0,report
    assert calls=={'main':50,'judge':50}
    assert report['requested_turns']==50 and report['passed']==0
    actual=report['results'][0]['turns']
    assert len(actual)==50 and not actual[26]['passed'] and actual[-1]['passed']
    assert sum(not t['passed'] for t in actual)==1
