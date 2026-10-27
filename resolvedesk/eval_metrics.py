"""Actual recognizer measurements and generic deterministic turn assertions."""
import statistics

from resolvedesk.intent import CATALOG


def intent_report(results):
    """Reduce observed predictions. This function never substitutes a rule router."""
    labels=sorted({r['expected'] for r in results}|{r['prediction'] for r in results if r.get('prediction')})
    per_class={}
    for label in labels:
        tp=sum(r['expected']==label==r.get('prediction') for r in results)
        predicted=sum(r.get('prediction')==label for r in results)
        expected=sum(r['expected']==label for r in results)
        precision,recall=tp/predicted if predicted else 0,tp/expected if expected else 0
        per_class[label]={'precision':precision,'recall':recall,'f1':2*precision*recall/(precision+recall) if precision+recall else 0,'support':expected}
    n=len(results)
    return {'implementation':'ITIntentRecognizer.recognize','samples':n,'accuracy':sum(r.get('prediction')==r['expected'] for r in results)/n if n else None,
        'macro_f1':statistics.mean(r['f1'] for r in per_class.values()) if per_class else None,'per_class':per_class,'results':results,
        'evaluation_errors':sum(bool(r.get('error')) for r in results),
        'channel_coverage':{name:sum(bool(r.get('sources',{}).get(name,{}).get('available')) for r in results) for name in ('llm','embedding','pattern')}}


async def measure_intent(recognizer,case):
    result=await recognizer.recognize(case['message'],case.get('history',[]),case.get('context',{}))
    expected=case['expected_intent']
    prediction=result.fine_intent if expected in CATALOG else result.domain
    return {'id':case['id'],'message':case['message'],'expected':expected,'prediction':prediction,'domain':result.domain,
        'fine_intent':result.fine_intent,'confidence':result.confidence,'sources':result.source_scores,'method':result.method,
        'passed':prediction==expected,'error':None}


def assertions(expected,run,events,entity_state):
    routes=[e['payload'] for e in events if e['kind']=='routing']
    route=routes[-1] if routes else {}
    checks={'execution_status':run['status']not in {'failed','paused','queued','running'}}
    if expected.get('expected_status')is not None:checks['status']=run['status']==expected['expected_status']
    if expected.get('expected_agent')is not None:checks['agent']=route.get('primary')==expected['expected_agent']
    label=expected.get('expected_intent')
    if label is not None:checks['intent']=route.get('fine_intent' if label in CATALOG else 'intent')==label
    used={e['payload'].get('name') for e in events if e['kind']=='tool_result' and e['payload'].get('success')}
    used|={t.get('tool_name') for e in events if e['kind']=='specialist_result' for t in e['payload'].get('tool_traces',[]) if t.get('success') and t.get('result_success')is not False}
    if expected.get('required_tools'):checks['tools']=set(expected['required_tools'])<=used
    if expected.get('expected_entities'):checks['entities']=all(set(values)<=set(entity_state.get('entities',{}).get(key,[])) for key,values in expected['expected_entities'].items())
    if expected.get('answer_contains'):checks['answer_contains']=all(s in run['answer'] for s in expected['answer_contains'])
    if expected.get('answer_excludes'):checks['answer_excludes']=all(s not in run['answer'] for s in expected['answer_excludes'])
    return checks,route,sorted(x for x in used if x)
