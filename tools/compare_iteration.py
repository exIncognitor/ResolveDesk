"""Compare immutable heldout and component evidence without changing labels."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import statistics
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def load(path):return json.loads(path.read_text(encoding='utf-8'))


def billing(report):
    before,after=report['budget_before'],report['budget_after']
    return {'conservative_cny':str(Decimal(after['conservative_commitment_cny'])-Decimal(before['conservative_commitment_cny'])),
        'calls':sum(after['calls'].values())-sum(before['calls'].values()),
        'input_tokens':after['usage_input_tokens']-before['usage_input_tokens'],
        'output_tokens':after['usage_output_tokens']-before['usage_output_tokens'],
        'elapsed_s':report['finished']-report['started']}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--candidate-quality',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--reports',type=Path,default=ROOT/'reports')
    args=parser.parse_args()
    if args.output.exists():raise ValueError('Refuse to replace comparison evidence')
    base=load(args.reports/'iteration2-baseline-quality.json');candidate=load(args.candidate_quality)
    old_rag=load(args.reports/'iteration2-baseline-retrieval.json');new_rag=load(args.reports/'iteration2-candidate-retrieval.json')
    old_perf=load(args.reports/'iteration2-baseline-performance.json');new_perf=load(args.reports/'iteration2-candidate-performance.json')
    def deterministic(case):return all(case.get('checks',{}).values()) and all(all(turn['checks'].values()) for turn in case['turns'])
    previous={case['test_id']:case for case in base['quality']['results']}
    current={case['test_id']:case for case in candidate['quality']['results']}
    lost=[name for name,case in previous.items() if deterministic(case) and (name not in current or not deterministic(current[name]))]
    checks={'same_quality_contract':base['contract_id']==candidate['contract_id'],
        'same_dialog_suite':base['quality']['suite_id']==candidate['quality']['suite_id'],
        'complete_intent_coverage':candidate['intent']['samples']==112 and candidate['intent']['evaluation_errors']==0,
        'intent_accuracy_non_regression':candidate['intent']['accuracy']>=base['intent']['accuracy'],
        'intent_macro_f1_non_regression':candidate['intent']['macro_f1']>=base['intent']['macro_f1'],
        'dialog_execution_complete':candidate['quality']['total']==12 and candidate['quality']['evaluation_errors']==0,
        'no_new_deterministic_dialog_failures':not lost,
        'auxiliary_judge_pass_rate_non_regression':candidate['quality']['passed']>=base['quality']['passed'],
        'same_fault_triggered':candidate['faults']['all_injected'] and candidate['faults']['injected']==base['faults']['injected'],
        'same_retrieval_contract':old_rag['contract_id']==new_rag['contract_id'],
        'same_all_300_document_rankings':len(old_rag['results'])==len(new_rag['results'])==300 and all(a['documents']==b['documents'] for a,b in zip(old_rag['results'],new_rag['results'])),
        'current_citations_and_spans_valid':all(r['citation_valid'] and r['span_valid'] for r in new_rag['results']),
        'same_performance_contract':old_perf['contract']==new_perf['contract'],
        'all_performance_profiles_present':len(old_perf['profiles'])==len(new_perf['profiles'])==12}
    performance=[]
    for before,after in zip(old_perf['profiles'],new_perf['profiles']):
        key=(before['chunks'],before['concurrency'],before['phase'])
        assert key==(after['chunks'],after['concurrency'],after['phase'])
        performance.append({'chunks':key[0],'concurrency':key[1],'phase':key[2],'baseline_p50_ms':before['p50_ms'],'candidate_p50_ms':after['p50_ms'],
            'baseline_p95_ms':before['p95_ms'],'candidate_p95_ms':after['p95_ms'],'baseline_error_rate':before['error_rate'],'candidate_error_rate':after['error_rate'],
            'p95_improvement_fraction':1-after['p95_ms']/before['p95_ms'],'baseline_cpu_s':before['process_cpu_s'],'candidate_cpu_s':after['process_cpu_s']})
    checks['all_component_p95_and_errors_non_regression']=all(p['candidate_p95_ms']<=p['baseline_p95_ms'] and p['candidate_error_rate']<=p['baseline_error_rate'] for p in performance)
    costs={'baseline':billing(base),'candidate':billing(candidate)}
    report={'status':'pass' if all(checks.values()) else 'fail','checks':checks,'policies':candidate.get('policies',{}),
        'baseline_quality':{'accuracy':base['intent']['accuracy'],'macro_f1':base['intent']['macro_f1'],'dialogs':base['quality']['passed']},
        'candidate_quality':{'accuracy':candidate['intent']['accuracy'],'macro_f1':candidate['intent']['macro_f1'],'dialogs':candidate['quality']['passed']},
        'lost_deterministic_dialogs':lost,'costs':costs,'performance':performance,
        'limitations':'Single stochastic Flash run per revision; same-model Judge correlated. Synthetic quality set and synthetic-vector component load are separate. Cold phase does not flush SQL/Chroma OS caches. Time and provider drift are not causal estimates.',
        'decision':'Adaptive policies may become default only if this comparison passes; do not tune on holdout failures. Failed candidates and raw reports must remain retained.'}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as out:json.dump(report,out,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in report.items() if k not in {'performance','decision','limitations'}},ensure_ascii=False))
    if report['status']!='pass':raise SystemExit(1)


if __name__=='__main__':main()
