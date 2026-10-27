"""Free deployed roundtrip; retire only the new synthetic job created by this run."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import subprocess
import sys
import time

import httpx

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply-synthetic',action='store_true')
    parser.add_argument('--base',choices=['http://127.0.0.1:8080','http://127.0.0.1:18080'],default='http://127.0.0.1:18080')
    args=parser.parse_args()
    if not args.apply_synthetic:parser.error('Use --apply-synthetic to verify retirement of this run\'s new synthetic job')
    access=json.loads((ROOT/'.codex-runtime/access.json').read_text(encoding='utf-8'))
    checks=[];report={'base':args.base,'checks':checks,'scope':'Only this probe-created free retrieval evaluation is retired. Existing evaluations, baselines and datasets remain intact.'}
    with httpx.Client(base_url=args.base,timeout=30,trust_env=False) as http:
        def request(method,path,body=None):
            result=http.request(method,path,json=body,headers={'Authorization':'Bearer '+access['support']['token']})
            result.raise_for_status();return result.json()
        before=request('GET','/system')['budget'];report['budget_before']=before
        try:
            actor=request('GET','/me')
            job=request('POST','/eval/run',{'kind':'retrieval'})
            job_id=job['job_id'];report['job_id']=job_id
            deadline=time.monotonic()+90
            while job['status'] in {'queued','running'} and time.monotonic()<deadline:
                time.sleep(.2);job=request('GET','/eval/jobs/'+job_id)
            assert job['status']=='completed'
            preview=request('GET','/eval/retention?job_id='+job_id)
            assert len(preview['plans'])==1 and preview['plans'][0]['job_id']==job_id
            assert preview['plans'][0]['resources'] is None
            checks.append({'name':'current_api_preview_exact_new_job','passed':True})
            command=[sys.executable,'-X','utf8','-B','tools/retain_evaluations.py','--tenant',actor['tenant'],'--owner',actor['user'],
                '--job-id',job_id,'--confirmation',preview['plans'][0]['confirmation'],'--apply']
            applied=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=120,check=True)
            result=json.loads(applied.stdout);report['retirement']=result
            checks.append({'name':'cli_verified_backup_and_retirement','passed':result['status']=='expired' and result['job_id']==job_id and bool(result['backup']['sha256'])})
            retired=request('GET','/eval/jobs/'+job_id)
            checks.append({'name':'report_tombstone_readable','passed':retired['status']=='expired' and retired['result'] is None})
            for action in ('resume','baseline'):
                response=http.post('/eval/jobs/'+job_id+'/'+action,headers={'Authorization':'Bearer '+access['support']['token']})
                checks.append({'name':action+'_rejects_expired','passed':response.status_code==409})
        except Exception as exc:
            report['error']=type(exc).__name__;checks.append({'name':'probe_execution','passed':False})
        finally:report['budget_after']=request('GET','/system')['budget']
    checks.append({'name':'no_paid_calls','passed':before==report['budget_after']})
    report['passed']=sum(c['passed'] for c in checks);report['total']=len(checks)
    path=ROOT/'reports'/('eval-retention-deployment-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    with path.open('x',encoding='utf-8') as dest:json.dump(report,dest,ensure_ascii=False,indent=2)
    print(json.dumps({'passed':report['passed'],'total':report['total'],'evidence':str(path.relative_to(ROOT))},ensure_ascii=False))
    if report['passed']!=report['total']:raise SystemExit(1)


if __name__=='__main__':main()
