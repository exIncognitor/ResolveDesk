"""Real host/container shared PostgreSQL budget probe; no model, no real ledger."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from sqlalchemy import text
    from core.financial_ledger import PostgresCostLedger
    from core.paid_api import CostLedger,BudgetPolicy,PaidCallBlocked
    from resolvedesk.settings import ServiceSettings
    identity=uuid.uuid4().hex
    schema='verify_finance_'+identity
    directory=ROOT/'.codex-tmp'/('finance-'+identity)
    for item in (directory,*directory.parents):
        if item.is_symlink() or item.is_junction():raise ValueError('Fixture path follows link')
    directory.mkdir(exist_ok=False)
    source=CostLedger(directory/'usage.sqlite3',BudgetPolicy())
    ledger=PostgresCostLedger(ServiceSettings.load().database_url,BudgetPolicy(),schema=schema)
    ledger.import_snapshot(source.path)
    code='''
import json,sys,time
from core.financial_ledger import PostgresCostLedger
from core.paid_api import BudgetPolicy,PaidCallBlocked
from resolvedesk.settings import ServiceSettings
ledger=PostgresCostLedger(ServiceSettings.load().database_url,BudgetPolicy(),schema=sys.argv[1])
accepted=[];blocked=0;errors=[]
for _ in range(80):
 try:
  call=ledger.reserve(100000,0);ledger.settle(call,100000,0);accepted.append(call)
 except PaidCallBlocked:blocked+=1
 except Exception as exc:errors.append(type(exc).__name__)
 time.sleep(.003)
print(json.dumps({'accepted':accepted,'blocked':blocked,'errors':errors}))
ledger.close()
'''
    def host():
        import time
        accepted=[];blocked=0;errors=[]
        for _ in range(80):
            try:
                call=ledger.reserve(100000,0);ledger.settle(call,100000,0);accepted.append(call)
            except PaidCallBlocked:blocked+=1
            except Exception as exc:errors.append(type(exc).__name__)
            time.sleep(.003)
        return {'accepted':accepted,'blocked':blocked,'errors':errors}
    def container():
        result=subprocess.run(['docker','exec','resolvedesk-services-app-1','python','-c',code,schema],capture_output=True,text=True,timeout=60,check=True)
        return json.loads(result.stdout)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(host),pool.submit(container)]
            results=[future.result() for future in futures]
        with ledger._transaction() as db:
            actual={row[0] for row in db.execute('SELECT id FROM paid_calls')}
        expected=set().union(*(set(item['accepted']) for item in results))
        summary=ledger.summary()
        passed=not any(r['errors'] for r in results) and actual==expected and len(actual)==90 and summary['conservative_commitment_cny']=='18' and summary['calls']=={'settled':90}
        report={'passed':passed,'synthetic_only':True,'paid_api_called':False,'backend':'postgresql',
            'host':{k:len(v) if k=='accepted' else v for k,v in results[0].items()},
            'container':{k:len(v) if k=='accepted' else v for k,v in results[1].items()},'rows':len(actual),'summary':summary}
        evidence=ROOT/'reports'/('financial-runtime-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
        with evidence.open('x',encoding='utf-8') as dest:json.dump(report,dest,ensure_ascii=False,indent=2)
        print(json.dumps({'passed':passed,'rows':len(actual),'evidence':str(evidence.relative_to(ROOT))}))
        if not passed:raise SystemExit(1)
    finally:
        with ledger.database.engine.begin() as db:
            tables=set(db.execute(text('SELECT tablename FROM pg_tables WHERE schemaname=:s'),{'s':schema}).scalars())
            if not schema.startswith('verify_finance_') or not tables<={'paid_calls','finance_authority'}:raise RuntimeError('Unexpected synthetic financial schema')
            db.exec_driver_sql('DROP SCHEMA "'+schema+'" CASCADE')
        ledger.close()
        allowed={source.path,source.path.with_name('usage.sqlite3-journal')}
        present=set(directory.iterdir())
        if not present<=allowed or any(p.is_symlink() or p.is_junction() or not p.is_file() for p in present):raise RuntimeError('Unexpected fixture contents')
        for item in present:item.unlink()
        directory.rmdir()


if __name__=='__main__':main()
