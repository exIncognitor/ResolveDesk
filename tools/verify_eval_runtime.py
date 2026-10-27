"""Opt-in real Flash evaluation via deployed current APIs; keep failure evidence."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
import time

import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--base',choices=['http://127.0.0.1:8080','http://127.0.0.1:18080'],default='http://127.0.0.1:18080')
    args=parser.parse_args()
    if not args.live:parser.error('Use --live for the authorized budgeted Flash probe')
    access=json.loads((ROOT/'.codex-runtime/access.json').read_text(encoding='utf-8'))
    from resolvedesk.quality import default_dataset
    dataset=default_dataset()
    dataset['title']='G08 当前入口真实评测验收'
    dataset['dialog_cases']=[{
        'id':'access-live','turns':[
            {'message':'我要申请仓库权限，暂时没有提供资源、权限级别、有效期和用途。请先询问必要信息，不要创建草稿。',
             'expected_status':'awaiting_input','required_tools':['ask_clarification'],
             'reference':'当前缺少资源、权限级别、有效期和用途，应该询问必要信息，不能虚构字段或声称申请成功。'},
            {'message':'请准备权限申请：资源 repo-resolvedesk，read 权限，7天，用途是阅读项目代码进行学习，标题为开发仓库只读权限申请。',
             'expected_status':'awaiting_confirmation','required_tools':['prepare_ticket'],
             'expected_entities':{'resource_id':['repo-resolvedesk'],'access_level':['read'],'duration_days':['7']},
             'reference':'只能准备草稿，显示资源 repo-resolvedesk、read、7天和用途，等待申请人核对确认；不能声称已开通。'}],
        'expected_agent':'general','expected_status':'awaiting_confirmation','workflow':'access_request',
        'expected_fields':{'resource_id':'repo-resolvedesk','access_level':'read','duration_days':7},'documents':['permissions']
    },{
        'id':'hazard-live','question':'笔记本电池鼓包了，请说明现在的安全处理办法和维修规则，不创建工单，并引用知识库。',
        'expected_agent':'escalation','expected_status':'completed','documents':['devices'],
        'reference_answer':'立即停止使用，断开充电，不挤压或拆卸，联系 IT 升级处理。维修申请需要本人资产ID和故障资料，不承诺真实维修结果。'
    }]
    checks=[];report={'base':args.base,'checks':checks,'provenance':'合成开发数据；真实 Flash 与真实服务；不是生产效果证明。'}
    with httpx.Client(base_url=args.base,timeout=30,trust_env=False) as http:
        def request(method,path,body=None,actor='support'):
            result=http.request(method,path,json=body,headers={'Authorization':'Bearer '+access[actor]['token']})
            result.raise_for_status();return result.json()
        before=request('GET','/system')['budget'];report['budget_before']=before
        try:
            assert request('GET','/ready')['ready']
            suite=request('POST','/eval/datasets',dataset)
            report['dataset_id']=suite['id']
            fetched=request('GET','/eval/datasets/'+suite['id'])
            checks.append({'name':'saved_dataset_roundtrip','passed':fetched['fingerprint']==suite['fingerprint']})
            job=request('POST','/eval/run',{'kind':'quality','suite_id':suite['id'],'limit':2,'timeout_s':540})
            report['job_id']=job['job_id']
            deadline=time.monotonic()+600
            while job['status'] in {'queued','running'} and time.monotonic()<deadline:
                time.sleep(1)
                job=request('GET','/eval/jobs/'+job['job_id'])
            report['job']=job
            result=job.get('result') or {}
            checks.extend([
                {'name':'completed_without_evaluation_errors','passed':job['status']=='completed' and result.get('evaluation_errors')==0},
                {'name':'same_formal_technologies','passed':result.get('sandbox',{}).get('backend')=='postgresql' and result.get('sandbox',{}).get('isolated') is True},
                {'name':'actual_28_intents_all_channels','passed':result.get('intent',{}).get('samples')==28 and result.get('intent',{}).get('implementation')=='ITIntentRecognizer.recognize' and result.get('intent',{}).get('channel_coverage')=={'llm':28,'embedding':28,'pattern':28}},
                {'name':'judge_every_requested_turn','passed':sum(len(d.get('turns',[])) for d in result.get('results',[]))==3 and all(t.get('judge',{}).get('attempts')==1 for d in result.get('results',[]) for t in d.get('turns',[]))},
                {'name':'all_dialog_business_and_quality_checks','passed':result.get('passed')==2 and result.get('total')==2},
            ])
        except Exception as exc:
            report['error']=type(exc).__name__
            checks.append({'name':'probe_execution','passed':False})
        finally:report['budget_after']=request('GET','/system')['budget']
    report['passed']=sum(c['passed'] for c in checks);report['total']=len(checks)
    path=ROOT/'reports'/('eval-runtime-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    with path.open('x',encoding='utf-8') as dest:json.dump(report,dest,ensure_ascii=False,indent=2)
    print(json.dumps({'passed':report['passed'],'total':report['total'],'job_id':report.get('job_id'),'evidence':str(path.relative_to(ROOT)),
        'budget_after':report['budget_after']},ensure_ascii=False))
    if report['passed']!=report['total']:raise SystemExit(1)


if __name__=='__main__':main()
