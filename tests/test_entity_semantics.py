"""Generic semantic and provenance properties, independent of frozen examples."""
import json
from pathlib import Path

import pytest

from resolvedesk.entities import extract,chinese_integer
from resolvedesk.entity_state import EntityState
from resolvedesk.store import Store
from test_resolvedesk import USER,OTHER


def test_all_development_examples_and_source_spans():
    path=Path(__file__).resolve().parents[1]/'evaluation/iteration3_entities_dev.json'
    for case in json.loads(path.read_text(encoding='utf-8'))['cases']:
        parsed=extract(case['text'])
        assert parsed['values']==case['expected'],case['id']
        for key,spans in parsed['spans'].items():
            assert {span['value'] for span in spans}==set(parsed['values'][key])
            assert all(case['text'][span['start']:span['end']]==span['text'] for span in spans)


@pytest.mark.parametrize('text,expected',[('十',10),('十九',19),('两百零六',206),('壹拾伍',15),('一百二',None),('一二',None)])
def test_integer_normalization_has_explicit_unambiguous_grammar(text,expected):assert chinese_integer(text)==expected


def test_independent_fields_and_all_alternatives_survive():
    parsed=extract('read改为write，期限改为十六天或十八天。')
    assert parsed['values']=={'access_level':['write'],'duration_days':['16','18']}
    assert extract('不要改动read，期限换为十六天')['values']=={'access_level':['read'],'duration_days':['16']}
    assert extract('不要admin和write，只要read')['values']=={'access_level':['read']}


def test_cleared_values_are_not_kept_and_negative_switch_is_not_a_reset(tmp_path):
    store=Store(tmp_path/'semantic.sqlite3');states=EntityState(store)
    try:
        run,_=store.create_run(USER,'docs-internal read十六天','semantic-initial')
        first=states.merge(USER,run['id'],run['message'],'account_access')
        store.claim(USER,run['id']);store.finish(USER,run['id'],'awaiting_input','继续')
        store.reply(USER,run['id'],'read先不要，期限不变','semantic-clear-level')
        second=states.merge(USER,run['id'],'read先不要，期限不变','account_access')
        assert 'access_level' not in second['entities'] and second['entities']['duration_days']==['16']
        assert second['sources']['duration_days']==first['sources']['duration_days']
        store.claim(USER,run['id']);store.finish(USER,run['id'],'awaiting_input','继续')
        store.reply(USER,run['id'],'不要换个问题，继续之前申请','semantic-no-switch')
        same=states.merge(USER,run['id'],'不要换个问题，继续之前申请','account_access')
        assert not same['reset'] and same['entities']==second['entities']
        with pytest.raises(KeyError):states.get(OTHER,run['id'])
    finally:store.close()


@pytest.mark.asyncio
async def test_production_prepare_cannot_invent_unknown_days(tmp_path):
    from resolvedesk.agent import DeskAgent
    from resolvedesk.retrieval import HybridKnowledge
    from resolvedesk.services import DeskServices
    from test_it_execution import model_for
    from test_it_workflows import ACCESS
    from test_resolvedesk import SUPPORT,response
    store=Store(tmp_path/'no-inferred-duration.sqlite3');kb=HybridKnowledge(store)
    kb.add(SUPPORT,'permissions','权限制度','# 权限\n申请需要资源、级别、用途和明确期限，模型不得自行选择。')
    model=model_for(fine=('access_request',));original=model.messages.create.side_effect
    async def create(**kwargs):
        if kwargs.get('system','').startswith('你是 ResolveDesk'):
            return response(tool='prepare_ticket',args=ACCESS|{'resource_id':'docs-internal','duration_days':14})
        return await original(**kwargs)
    model.messages.create.side_effect=create
    service=DeskServices(store,kb,model,mcp_configs=[]);agent=DeskAgent(store,kb,model,service)
    try:
        run,_=store.create_run(USER,'请准备docs-internal read访问申请，用途是学习接口，使用天数尚未决定。','no-inferred-days')
        await agent.execute(USER,run['id']);current=store.get_run(USER,run['id'])
        assert current['status']=='awaiting_input' and current['draft']['duration_days']==0
        assert '有效天数' in current['answer'] and not current['approval'] and not store.list_tickets(USER)
    finally:await agent.close();store.close()
