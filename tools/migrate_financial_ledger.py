"""Import an intact legacy ledger after stopping host and container paid clients.

Preserve SQLite and PostgreSQL snapshots first. Never resets/reimports an
existing authority. Only counts and digests are printed, never credentials.
"""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if not args.apply:parser.error('Use --apply only after stopping all paid clients')
    # The application container must be stopped. The caller separately verifies
    # and stops its owned host process before invoking this local operator tool.
    running=subprocess.run(['docker','inspect','--format','{{.State.Running}}','resolvedesk-services-app-1'],capture_output=True,text=True,check=True,timeout=10)
    if running.stdout.strip()!='false':raise RuntimeError('Stop the app container before financial cutover')
    from tools.migrate_services import snapshot,validate_path
    from resolvedesk.settings import ServiceSettings
    from core.financial_ledger import PostgresCostLedger
    from core.paid_api import BudgetPolicy
    directory=validate_path(ROOT/'.codex-runtime/checkpoints'/('finance-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid.uuid4().hex[:8]))
    directory.mkdir(parents=True,exist_ok=False)
    source=snapshot(ROOT/'data/billing/usage.sqlite3',directory/'usage.sqlite3')
    with (directory/'resolvedesk.dump').open('xb') as dest:
        subprocess.run(['docker','exec','resolvedesk-services-postgres-1','pg_dump','-U','resolvedesk','-d','resolvedesk','-Fc'],stdout=dest,stderr=subprocess.PIPE,check=True,timeout=60)
    if (directory/'resolvedesk.dump').stat().st_size<100:raise RuntimeError('Empty PostgreSQL backup')
    ledger=PostgresCostLedger(ServiceSettings.load().database_url,BudgetPolicy())
    try:
        result=ledger.import_snapshot(source)
        report={'imported':result,'budget':ledger.summary(),'source_preserved':True,'snapshot':str(directory.relative_to(ROOT))}
        with (directory/'migration-report.json').open('x',encoding='utf-8') as dest:json.dump(report,dest,ensure_ascii=False,indent=2)
        evidence=ROOT/'reports'/('financial-migration-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
        with evidence.open('x',encoding='utf-8') as dest:json.dump(report,dest,ensure_ascii=False,indent=2)
        print(json.dumps(report,ensure_ascii=False))
    finally:ledger.close()


if __name__=='__main__':main()
