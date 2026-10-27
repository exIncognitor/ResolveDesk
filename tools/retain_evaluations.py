"""Preview or explicitly retire ONE evaluation after a verified full PG backup."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def backup_local_compose(store,root=ROOT):
    from resolvedesk.eval_retention import VerifiedArchive
    url=urlsplit(store.database.engine.url.render_as_string(hide_password=False))
    if url.hostname!='127.0.0.1' or url.port!=15432 or url.path!='/resolvedesk':raise ValueError('Apply requires local Compose backup; remote stores need their own verified archive')
    directory=root/'.codex-runtime/checkpoints'/('eval-retention-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid.uuid4().hex[:8])
    for part in (directory,*directory.parents):
        if part.is_symlink() or part.is_junction():raise ValueError('Unexpected backup link')
    directory.mkdir(parents=True,exist_ok=False)
    path=directory/'resolvedesk.dump'
    with path.open('xb') as dest:
        result=subprocess.run(['docker','exec','resolvedesk-services-postgres-1','pg_dump','-U','resolvedesk','-d','resolvedesk','-Fc'],stdout=dest,stderr=subprocess.PIPE,timeout=60)
    if result.returncode or path.stat().st_size<100:raise RuntimeError('PostgreSQL backup failed')
    with path.open('rb') as source:
        checked=subprocess.run(['docker','exec','-i','resolvedesk-services-postgres-1','pg_restore','-l'],stdin=source,capture_output=True,timeout=30)
    if checked.returncode:raise RuntimeError('PostgreSQL archive verification failed')
    schemas=set(re.findall(r'\bTABLE\s+([a-z][a-z0-9_]*)\s+',checked.stdout.decode('utf-8')))
    if store.database.schema not in schemas:raise RuntimeError('Archive does not cover source schema')
    return VerifiedArchive(path,hashlib.sha256(path.read_bytes()).hexdigest(),schemas)


def main():
    from resolvedesk.eval_retention import EvalRetention,ServiceResourceCleaner
    from resolvedesk.settings import ServiceSettings
    from resolvedesk.store import Principal,Store
    parser=argparse.ArgumentParser()
    parser.add_argument('--tenant',required=True);parser.add_argument('--owner',required=True)
    parser.add_argument('--days',type=int,default=30)
    parser.add_argument('--job-id');parser.add_argument('--confirmation')
    parser.add_argument('--apply',action='store_true',help='Discard exactly the previewed job and its verified isolated service resources')
    args=parser.parse_args()
    settings=ServiceSettings.load();store=Store(settings.database_url)
    cleaner=None
    try:
        operator=Principal(args.tenant,args.owner,'support');retention=EvalRetention(store)
        preview=retention.preview(operator,days=args.days,job_id=args.job_id)
        if not args.apply:print(json.dumps(preview,ensure_ascii=False));return
        if not args.job_id or not args.confirmation:parser.error('--apply requires --job-id and the preview --confirmation')
        if len(preview['plans'])!=1 or preview['plans'][0]['confirmation']!=args.confirmation:raise ValueError('Preview does not authorize the selected job')
        archive=backup_local_compose(store)
        cleaner=ServiceResourceCleaner(settings.redis_url,settings.chroma_url)
        result=retention.apply(operator,args.job_id,args.confirmation,cleaner,archive)
        result['recovery_note']='Restore selected application evidence only; never rewind the shared financial ledger.'
        path=archive.path.parent/'retirement-report.json'
        with path.open('x',encoding='utf-8') as dest:json.dump(result,dest,ensure_ascii=False,indent=2)
        print(json.dumps(result,ensure_ascii=False))
    finally:
        if cleaner:cleaner.close()
        store.close()


if __name__=='__main__':main()
