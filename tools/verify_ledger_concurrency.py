"""Probe Windows/Linux shared SQLite locking using a NEW synthetic ledger only.

Never touches the real ledger and never contacts any model. Both processes
reserve until the fixed budget is exhausted; every accepted row must survive.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.paid_api import CostLedger,BudgetPolicy,PaidCallBlocked


def main():
    identity=uuid.uuid4().hex
    directory=ROOT/'data/billing'/('synthetic-concurrency-'+identity)
    for path in (directory,*directory.parents):
        if path.is_symlink() or path.is_junction():raise ValueError('Test path follows a link')
    directory.mkdir(exist_ok=False)
    path=directory/'usage.sqlite3'
    ledger=CostLedger(path,BudgetPolicy())
    container_path='/app/data/billing/'+directory.name+'/usage.sqlite3'
    code="""
import json,sys,time
from pathlib import Path
from core.paid_api import CostLedger,BudgetPolicy,PaidCallBlocked
ledger=CostLedger(Path(sys.argv[1]),BudgetPolicy())
accepted=[];blocked=0;errors=[]
for _ in range(80):
 try:
  call=ledger.reserve(100000,0);ledger.settle(call,100000,0);accepted.append(call)
 except PaidCallBlocked:blocked+=1
 except Exception as exc:errors.append(type(exc).__name__)
 time.sleep(.003)
print(json.dumps({'accepted':accepted,'blocked':blocked,'errors':errors}))
"""
    def host():
        accepted=[];blocked=0;errors=[]
        for _ in range(80):
            try:
                call=ledger.reserve(100000,0);ledger.settle(call,100000,0);accepted.append(call)
            except PaidCallBlocked:blocked+=1
            except Exception as exc:errors.append(type(exc).__name__)
            import time
            time.sleep(.003)
        return {'accepted':accepted,'blocked':blocked,'errors':errors}
    def container():
        result=subprocess.run(['docker','exec','resolvedesk-services-app-1','python','-c',code,container_path],capture_output=True,text=True,timeout=60,check=True)
        return json.loads(result.stdout)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs=[pool.submit(host),pool.submit(container)]
            results=[job.result() for job in jobs]
        with ledger._transaction() as db:
            actual={row[0] for row in db.execute('SELECT id FROM paid_calls')}
            integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
        expected=set().union(*(set(item['accepted']) for item in results))
        summary=ledger.summary()
        passed=not any(r['errors'] for r in results) and actual==expected and len(actual)==90 and integrity=='ok' and summary['conservative_commitment_cny']=='18'
        report={'passed':passed,'synthetic_only':True,'paid_api_called':False,'host':{k:len(v) if k=='accepted' else v for k,v in results[0].items()},
            'container':{k:len(v) if k=='accepted' else v for k,v in results[1].items()},'rows':len(actual),'integrity':integrity,'summary':summary}
        evidence=ROOT/'reports'/('ledger-concurrency-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
        with evidence.open('x',encoding='utf-8') as dest:json.dump(report,dest,ensure_ascii=False,indent=2)
        print(json.dumps({'passed':passed,'rows':len(actual),'evidence':str(evidence.relative_to(ROOT))}))
        if not passed:raise SystemExit(1)
    finally:
        # Exactly these self-created, disposable fixture files; never a glob or
        # recursive delete and never the parent data/billing directory.
        allowed={path,path.with_name(path.name+'-journal'),path.with_name(path.name+'-wal'),path.with_name(path.name+'-shm')}
        present=set(directory.iterdir())
        if not present<=allowed:raise RuntimeError('Unexpected synthetic fixture files')
        for item in present:
            if item.is_symlink() or item.is_junction() or not item.is_file():raise RuntimeError('Unsafe synthetic fixture')
        for item in present:item.unlink()
        directory.rmdir()


if __name__=='__main__':main()
