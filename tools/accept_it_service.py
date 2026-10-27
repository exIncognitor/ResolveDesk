"""Finite live acceptance; synthetic business requests are explicitly labeled."""
import argparse
import asyncio
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
from threading import Thread
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def webhook_probe(output):
    from resolvedesk.monitoring import Monitor
    from resolvedesk.store import Store
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200); self.end_headers()
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    previous = os.environ.get('RESOLVEDESK_ALERT_WEBHOOK')
    try:
        os.environ['RESOLVEDESK_ALERT_WEBHOOK'] = f'http://127.0.0.1:{server.server_port}/alerts'
        monitor = Monitor(Store(output/'monitor-probe.sqlite3'))
        for _ in range(3): monitor.record('synthetic-probe', 'agent', 'technical', False, .01)
        triggered = monitor.refresh('synthetic-probe')
        asyncio.run(monitor.deliver())
        for _ in range(4): monitor.record('synthetic-probe', 'agent', 'technical', True, .01)
        resolved = monitor.refresh('synthetic-probe')
        asyncio.run(monitor.deliver())
        return {'synthetic_failure_injection':True,'real_local_http_delivery':True,'received_states':[r['state'] for r in received],
                'passed':bool(triggered['routing_penalties']) and not resolved['routing_penalties'] and [r['state'] for r in received]==['active','resolved']}
    finally:
        if previous is None: os.environ.pop('RESOLVEDESK_ALERT_WEBHOOK', None)
        else: os.environ['RESOLVEDESK_ALERT_WEBHOOK'] = previous
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def main():
    import httpx
    parser = argparse.ArgumentParser()
    parser.add_argument('--quality', action='store_true')
    parser.add_argument('--job-id')
    parser.add_argument('--live-workflows', action='store_true')
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    scratch = ROOT/'.codex-runtime'/('accept-it-'+stamp)
    scratch.mkdir(parents=True,exist_ok=False)
    credentials=json.loads((ROOT/'.codex-runtime/access.json').read_text(encoding='utf-8'))
    checks=[]
    with httpx.Client(base_url='http://127.0.0.1:8080',timeout=20) as http:
        def request(actor,method,path,data=None):
            response=http.request(method,path,json=data,headers={'Authorization':'Bearer '+credentials[actor]['token']})
            response.raise_for_status();return response.json()
        def wait_job(job):
            print(json.dumps({'job_id':job['job_id'],'kind':'evaluation'},ensure_ascii=False),flush=True)
            deadline=time.monotonic()+900
            previous=None
            while time.monotonic()<deadline:
                job=request('support','GET','/eval/jobs/'+job['job_id'])
                progress=(job['status'],job['completed'],job['total'])
                if progress!=previous:print(json.dumps({'progress':progress}),flush=True);previous=progress
                if job['status'] not in {'running','queued'}:return job
                time.sleep(2)
            raise TimeoutError('Quality evaluation still running; use --job-id to retrieve it')
        if args.quality or args.job_id:
            job=wait_job({'job_id':args.job_id} if args.job_id else request('support','POST','/eval/run',{'kind':'quality'}))
            result=job.get('result') or {}
            checks.append({'case':'active_service_quality','passed':job['status']=='completed' and result.get('passed')==result.get('total')==6 and result.get('evaluation_errors')==0,
                           'job_id':job['job_id'],'result':result})
        retrieval=wait_job(request('support','POST','/eval/run',{'kind':'retrieval'}))
        result=retrieval.get('result') or {}
        checks.append({'case':'retrieval','passed':retrieval['status']=='completed' and result.get('passed')==result.get('total') and result.get('total',0)>=20,'job_id':retrieval['job_id'],'result':result})
        if args.live_workflows:
            requests=[('access_request','[模拟业务验收] 请准备权限申请：资源 repo-resolvedesk，read 权限，7天，用途是阅读项目代码学习，标题为开发仓库只读权限申请。'),
                      ('device_repair','[模拟业务验收] 请准备设备维修申请：本人资产 LT-1001，键盘失灵，从今天上午开始，已经检查外接设备，标题为笔记本键盘维修，hardware类别。')]
            for kind,message in requests:
                run=request('learner','POST','/runs',{'message':message,'request_key':'it-accept-'+uuid.uuid4().hex})
                deadline=time.monotonic()+110
                while time.monotonic()<deadline:
                    run=request('learner','GET','/runs/'+run['id'])
                    if run['status'] not in {'running','queued'}:break
                    time.sleep(.3)
                check={'case':'live-'+kind,'run_id':run['id'],'passed':False,'run_status':run['status']}
                if run['status']=='awaiting_confirmation' and run['draft'].get('workflow')==kind:
                    ticket=request('learner','POST',f"/runs/{run['id']}/confirm",{'approval':run['approval']})
                    case=request('learner','GET','/service-cases/'+ticket['id'])
                    actions=['approve','provision'] if kind=='access_request' else ['triage','assign','start','resolve','accept']
                    for index,action in enumerate(actions):
                        actor='learner' if action=='accept' else 'support'
                        case=request(actor,'POST',f"/service-cases/{ticket['id']}/actions",{'action':action,'version':case['version'],'request_key':'live-action-'+str(index),
                            'note':'[模拟验收] '+action,'assignee':'support' if action=='assign' else ''})
                    check.update({'ticket_id':ticket['id'],'final_state':case['status'],'audit':case['audit'],'passed':case['status'] in {'fulfilled','closed'}})
                checks.append(check)
                print(json.dumps({k:v for k,v in check.items() if k!='audit'},ensure_ascii=False),flush=True)
        checks.append({'case':'local_webhook_alert_recovery',**webhook_probe(scratch)})
        system=request('support','GET','/system')
        monitor=request('support','GET','/monitor')
    report={'created_utc':datetime.now(timezone.utc).isoformat(),'model':'deepseek-flash','synthetic_business_data':True,
            'all_passed':all(c['passed'] for c in checks),'checks':checks,'budget':system['budget'],'monitor':monitor}
    output=ROOT/'reports'/('it-service-acceptance-'+stamp+'.json')
    with output.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps({'report':str(output),'all_passed':report['all_passed'],'budget':report['budget'],'checks':[{'case':c['case'],'passed':c['passed']} for c in checks]},ensure_ascii=False,indent=2))
    if not report['all_passed']:raise SystemExit(1)


if __name__=='__main__':main()
