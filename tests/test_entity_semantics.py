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
