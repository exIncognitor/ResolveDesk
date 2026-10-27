"""Validate and freeze authored holdout bytes before any optimization result."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import unicodedata

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC',text).lower() if c.isalnum())


def contract(root=ROOT):
    from resolvedesk.eval_models import EvalDataset
    from resolvedesk.intent import CATALOG
    dataset=EvalDataset.model_validate(json.loads((root/'evaluation/holdout_v1.json').read_text(encoding='utf-8')))
    retrieval=json.loads((root/'evaluation/holdout_retrieval_v1.json').read_text(encoding='utf-8'))['cases']
    known=[]
    def messages(node):
        if isinstance(node,dict):
            for key,value in node.items():
                if key in {'message','query','question'} and isinstance(value,str):yield value
                else:yield from messages(value)
        elif isinstance(node,list):
            for value in node:yield from messages(value)
    for name in ('it_service_cases.json','it_intent_boundary_cases.json','retrieval_cases.json'):
        known.extend(messages(json.loads((root/'evaluation'/name).read_text(encoding='utf-8'))))
    known.extend(example for _,examples,_ in CATALOG.values() for example in examples)
    existing={normalized(text) for text in known}
    new=[case.message for case in dataset.intent_cases]
    new.extend(turn.message for case in dataset.dialog_cases for turn in case.turns)
    assert len(dataset.intent_cases)>=100 and len(dataset.dialog_cases)>=12 and len(retrieval)>=100
    assert all(len(case.turns)>=2 for case in dataset.dialog_cases)
    assert len({normalized(text) for text in new})==len(new),'Duplicate holdout utterance'
    assert not existing&{normalized(text) for text in new},'Development utterance leaked into holdout'
    assert len({case['id'] for case in retrieval})==len(retrieval)
    documents={path.stem for path in (root/'knowledge').glob('*.md')}
    assert all(case['relevant_documents'] and set(case['relevant_documents'])<=documents for case in retrieval)
    paths=[root/'evaluation'/name for name in ('holdout_v1.json','holdout_retrieval_v1.json','holdout_faults_v1.json')]
    # New business extension documents are deliberately outside this common-task
    # contract; the original seven knowledge files remain byte-for-byte frozen.
    paths.extend(root/'knowledge'/name for name in ('access.md','devices.md','endpoint.md','licensing.md','permissions.md','software.md','vpn.md'))
    return {'schema':'resolvedesk-holdout-contract-v1','baseline':'71ff1d13d11a6ee2d767fcdbce5d34afd849679a',
        'intent_cases':len(dataset.intent_cases),'retrieval_cases':len(retrieval),'dialogs':len(dataset.dialog_cases),
        'turns':sum(len(case.turns) for case in dataset.dialog_cases),'development_utterances_checked':len(existing),
        'sha256':{path.relative_to(root).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
        'quality_policy':'Do not tune on held-out failures or relabel after freeze; preserve raw baseline and candidate outcomes.',
        'limitations':'LLM-authored synthetic held-out regression set with exact normalized text deduplication, not independent third-party labeling, semantic deduplication or production evidence.'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
    result=contract();path=ROOT/'evaluation/holdout_manifest_v1.json'
    if args.check:
        assert json.loads(path.read_text(encoding='utf-8'))==result,'Frozen dataset or knowledge changed'
    else:
        with path.open('x',encoding='utf-8') as out:json.dump(result,out,ensure_ascii=False,indent=2)
    print(json.dumps({key:value for key,value in result.items() if key!='sha256'},ensure_ascii=False))


if __name__=='__main__':main()
