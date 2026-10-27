"""Operator-controlled retention preview/apply. Never scheduled automatically."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from resolvedesk.settings import ServiceSettings
from resolvedesk.store import Principal,Store
from resolvedesk.retention import Retention


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--tenant",required=True)
    parser.add_argument("--days",type=int,default=30)
    parser.add_argument("--apply",action="store_true",help="Explicitly discard eligible old trace/response bodies after a fresh database backup")
    args=parser.parse_args()
    settings=ServiceSettings.load()
    store=Store(settings.database_url)
    try:
        retention=Retention(store)
        actor=Principal(args.tenant,"local-maintenance","support")
        preview=retention.sweep(actor,days=args.days)
        if not args.apply:
            print(json.dumps(preview,ensure_ascii=False));return
        if not any(preview["counts"].values()):
            print(json.dumps(preview|{"note":"No eligible evidence; no database changes"},ensure_ascii=False));return
        url=urlsplit(settings.database_url)
        if url.hostname!="127.0.0.1" or url.port!=15432 or url.path!="/resolvedesk":
            raise ValueError("Apply requires the configured local Compose PostgreSQL; remote maintenance requires its own reviewed backup procedure")
        directory=ROOT/".codex-runtime"/"checkpoints"/("retention-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")+"-"+uuid.uuid4().hex[:8])
        for part in (directory,*directory.parents):
            if part.is_symlink() or part.is_junction(): raise ValueError("Unexpected backup link")
        directory.mkdir(parents=True,exist_ok=False)
        with (directory/"resolvedesk.dump").open("xb") as dest:
            subprocess.run(["docker","exec","resolvedesk-services-postgres-1","pg_dump","-U","resolvedesk","-d","resolvedesk","-Fc"],stdout=dest,stderr=subprocess.PIPE,check=True,timeout=60)
        if (directory/"resolvedesk.dump").stat().st_size<100: raise RuntimeError("Backup is empty; retention stopped")
        result=retention.sweep(actor,days=args.days,apply=True)
        result["recovery_snapshot"]=str(directory.relative_to(ROOT))
        with (directory/"retention-report.json").open("x",encoding="utf-8") as dest:json.dump(result,dest,ensure_ascii=False,indent=2)
        print(json.dumps(result,ensure_ascii=False))
    finally: store.close()


if __name__=="__main__": main()
