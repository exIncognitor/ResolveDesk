"""Free same-contract entity/state evaluation against an immutable engine."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--engine-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--dev',action='store_true')
    args=parser.parse_args();sys.path.insert(0,str(args.engine_root))
    from resolvedesk.intent import entities_from
    from resolvedesk.entity_state import EntityState
    from resolvedesk.store import Principal,Store
    if args.output.exists():raise ValueError('Refuse to replace evidence')
    contract=json.loads((ROOT/'evaluation/iteration3_entity_contract.json').read_text(encoding='utf-8'))
    for name,expected in contract['sha256'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=expected:raise ValueError('Frozen input changed')
    dataset=json.loads((ROOT/'evaluation'/('iteration3_entities_dev.json' if args.dev else 'iteration3_entities_holdout.json')).read_text(encoding='utf-8'))
    def checks(case,values):
        return {**{key:values.get(key)==expected for key,expected in case['expected'].items()},
            **{'absent:'+key:key not in values for key in case.get('absent',[])}}
    report={'contract':hashlib.sha256((ROOT/'evaluation/iteration3_entity_contract.json').read_bytes()).hexdigest(),
        'engine_root':str(args.engine_root),'paid_calls':0,'split':'development' if args.dev else 'frozen_acceptance','entities':[],'sequences':[]}
    for case in dataset['cases']:
        values=entities_from(case['text']);assertions=checks(case,values)
        report['entities'].append({'id':case['id'],'values':values,'checks':assertions,'passed':all(assertions.values())})
    fixture=ROOT/'.codex-tmp/iteration3'/('entity-eval-'+args.output.stem)
    fixture.mkdir(exist_ok=False)
    store=Store(fixture/'entities.sqlite3');user=Principal('entity-test','learner');states=EntityState(store)
    try:
        for case in dataset.get('sequences',[]):
            run,_=store.create_run(user,case['turns'][0]['text'],'v3-'+case['id']);turns=[]
            for index,turn in enumerate(case['turns']):
                if index:store.reply(user,run['id'],turn['text'],'v3-reply-'+str(index))
                state=states.merge(user,run['id'],turn['text'],turn['domain'])
                assertions=checks(turn,state['entities'])
                if 'reset' in turn:assertions['task_reset']=state['reset']==turn['reset']
                assertions['source_values_current']=all(source['source']=='explicit_user_text' and source['values']==state['entities'][field] and source['turn_seq']<=state['turn_seq'] for field,source in state['sources'].items())
                assertions['same_turn_idempotent']=states.merge(user,run['id'],turn['text'],turn['domain'])==state
                turns.append({'turn':index+1,'state':state,'checks':assertions,'passed':all(assertions.values())})
                store.claim(user,run['id']);store.finish(user,run['id'],'awaiting_input','合成状态机下一轮')
            report['sequences'].append({'id':case['id'],'turns':turns,'passed':all(turn['passed'] for turn in turns)})
    finally:store.close()
    report.update(entity_passed=sum(case['passed'] for case in report['entities']),entity_total=len(report['entities']),
        sequence_passed=sum(case['passed'] for case in report['sequences']),sequence_total=len(report['sequences']))
    with args.output.open('x',encoding='utf-8',newline='\n') as out:json.dump(report,out,ensure_ascii=False,indent=2)
    print(json.dumps({key:value for key,value in report.items() if key not in {'entities','sequences'}},ensure_ascii=False))


if __name__=='__main__':main()
