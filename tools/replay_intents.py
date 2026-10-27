"""Free paired replay of all observed Flash intent channels, not a new live score.

Reconstruct the validated JSON response for the unchanged recognizer parser;
keep real E5 and rule channels. This isolates implementation/embedding changes
from stochastic provider output. Never replaces or relabels live heldout data.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]


async def run(args):
    sys.path.insert(0,str(args.engine_root))
    from resolvedesk.intent import ITIntentRecognizer
    from resolvedesk.retrieval import LocalEmbedding
    from resolvedesk.eval_metrics import measure_intent
    if args.output.exists():raise ValueError('Refuse to replace replay evidence')
    dataset=json.loads((args.config_root/'evaluation/holdout_v1.json').read_text(encoding='utf-8'))
    cases={case['id']:case for case in dataset['intent_cases']}
    embedding=LocalEmbedding(args.config_root/'data/models/multilingual-e5-small')
    report={'contract':'observed-flash-channel-replay-v1','embedding':embedding.identity,'inputs':{},'results':[],
        'note':'Free component equivalence only; does not turn failed live comparisons into passing results, estimate fresh model quality, or permit adaptive policy promotion.'}
    for name in ('iteration2-baseline-quality.json','iteration2-candidate-adaptive-quality.json','iteration2-candidate-full-quality.json'):
        path=args.config_root/'reports'/name
        source=json.loads(path.read_text(encoding='utf-8'))
        report['inputs'][name]=hashlib.sha256(path.read_bytes()).hexdigest()
        original={item['message']:item for item in source['intent']['results']}
        async def create(**kwargs):
            prompt=json.loads(kwargs['messages'][0]['content']);channel=original[prompt['message']]['sources']['llm']
            if not channel['available']:raise ConnectionError('Replayed observed unavailable channel')
            labels=sorted(channel['scores'],key=channel['scores'].get,reverse=True)
            data={'fine_intents':labels,'confidence':channel['scores'][labels[0]],'urgency':channel.get('urgency',1),'reason':channel.get('reason','')}
            return SimpleNamespace(content=[{'type':'text','text':json.dumps(data,ensure_ascii=False)}],stop_reason='end_turn')
        recognizer=ITIntentRecognizer(SimpleNamespace(messages=SimpleNamespace(create=create)),embedding)
        for item in source['intent']['results']:
            case=cases[item['id']];value=await measure_intent(recognizer,case)
            report['results'].append({'trace':name,'id':case['id'],'prediction':value['prediction'],'confidence':value['confidence'],
                'observed_prediction':item['prediction'],'matches_observed':value['prediction']==item['prediction'],
                'embedding_scores':value['sources']['embedding']['scores'],'pattern_scores':value['sources']['pattern']['scores']})
    report['all_observed_predictions_reproduced']=all(item['matches_observed'] for item in report['results'])
    with args.output.open('x',encoding='utf-8') as out:json.dump(report,out,ensure_ascii=False,indent=2)
    print(json.dumps({'output':str(args.output),'samples':len(report['results']),'all_observed_predictions_reproduced':report['all_observed_predictions_reproduced']}))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--engine-root',type=Path,required=True)
    parser.add_argument('--config-root',type=Path,default=ROOT);parser.add_argument('--output',type=Path,required=True)
    asyncio.run(run(parser.parse_args()))


if __name__=='__main__':main()
