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
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--apply',action='store_true',help='Import an existing intact legacy ledger')
    mode.add_argument('--initialize-new',action='store_true',help='First installation only; refuses any existing legacy ledger')
    args=parser.parse_args()
    # The application container must be stopped. The caller separately verifies
    # and stops its owned host process before invoking this local operator tool.
    running=subprocess.run(['docker','ps','--filter','name=^/resolvedesk-services-app-1$','--format','{{.Names}}'],capture_output=True,text=True,check=True,timeout=10)
    if running.stdout.strip():raise RuntimeError('Stop the app container before financial cutover')
    from tools.migrate_services import snapshot,validate_path
    from resolvedesk.settings import ServiceSettings
    from core.financial_ledger import PostgresCostLedger
    from core.paid_api import BudgetPolicy,CostLedger
    legacy=validate_path(ROOT/'data/billing/usage.sqlite3')
    if args.initialize_new:
        if legacy.exists():raise RuntimeError('A legacy ledger already exists; use --apply, never reset it')
        # Explicit first-install action only. Runtime cannot initialize or reset
        # a missing authority simply by changing an environment variable.
        CostLedger(legacy,BudgetPolicy())
    directory=validate_path(ROOT/'.codex-runtime/checkpoints'/('finance-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid.uuid4().hex[:8]))
    directory.mkdir(parents=True,exist_ok=False)
    source=snapshot(legacy,directory/'usage.sqlite3')
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
