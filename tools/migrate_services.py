"""Copy legacy SQLite into an EMPTY PostgreSQL target, preserving all evidence.

Creates consistent independent SQLite and cost-ledger snapshots first. Never
truncates, overwrites, or resets either source or an existing target. Reports
only counts/digests, not row content, tokens or connection credentials.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from resolvedesk.store import Store,Principal,encode,digest
from resolvedesk.memory import MEMORY_TABLES
from resolvedesk.schema_tables import APPLICATION_TABLES

TABLES=MEMORY_TABLES|{"runs","events","messages","tickets","feedback","checkpoints","turn_receipts","entity_state","desk_assets","service_cases","service_audit","service_actions","desk_grants",
    "chunks","knowledge_documents","index_outbox","memory_summary","memory_episode","memory_profile","eval_jobs","regression_cases","quality_baselines","telemetry","desk_alerts","alert_outbox",
    'eval_suites','eval_snapshots','eval_units','eval_queue_guard'}
TABLES=APPLICATION_TABLES


def validate_path(path):
    path=Path(path).absolute()
    for part in (path,*path.parents):
        if part.is_symlink() or part.is_junction(): raise ValueError("Snapshot paths must not follow links")
    return path


def sqlite_manifest(db):
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if not tables<=TABLES: raise ValueError("Unexpected legacy application tables")
    result={}
    for table in sorted(tables):
        columns=[r[1] for r in db.execute('PRAGMA table_info("'+table+'")')]
        rows=[dict(zip(columns,row)) for row in db.execute('SELECT * FROM "'+table+'"')]
        result[table]={"columns":columns,**row_digest(rows)}
    return result


def row_digest(rows):
    hashes=sorted(hashlib.sha256(encode(row).encode()).hexdigest() for row in rows)
    return {"count":len(rows),"sha256":hashlib.sha256("\n".join(hashes).encode()).hexdigest()}


def snapshot(source,destination,application=False):
    source,destination=validate_path(source),validate_path(destination)
    if not source.is_file() or destination.exists(): raise ValueError("Missing source or occupied snapshot destination")
    # Exclusive creation prevents a retry from replacing earlier evidence.
    with destination.open("xb"): pass
    with sqlite3.connect(source.as_uri()+"?mode=ro",uri=True,timeout=10) as src,sqlite3.connect(destination) as dst:
        src.execute("BEGIN")
        before=sqlite_manifest(src) if application else None
        src.backup(dst)
        assert dst.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
        if application and sqlite_manifest(dst)!=before: raise RuntimeError("SQLite snapshot content mismatch")
    return destination


def initialize(target):
    from resolvedesk.retrieval import HybridKnowledge
    from resolvedesk.services import ScopedMemory
    from resolvedesk.evaluations import EvaluationService
    from resolvedesk.monitoring import Monitor
    HybridKnowledge(target)
    ScopedMemory(target,None,None)
    EvaluationService(target,None)
    Monitor(target)


def copy_to_postgres(source,target):
    if target.backend!="postgresql": raise ValueError("Migration target must be PostgreSQL")
    with sqlite3.connect(Path(source).as_uri()+"?mode=ro",uri=True) as src,target.db(True) as db:
        manifest=sqlite_manifest(src)
        actual={r[0] for r in db.execute("SELECT tablename FROM pg_tables WHERE schemaname=?",(db.schema,))}
        if not actual<=TABLES: raise ValueError("Unexpected target tables; refusing migration")
        for table in sorted(actual):
            if table in {'memory_queue_guard','eval_queue_guard'}:
                if [tuple(row) for row in db.execute('SELECT * FROM "'+table+'"')]!=[(1,)]:raise ValueError("Unexpected queue guard seed")
                continue
            if db.execute('SELECT COUNT(*) FROM "'+table+'"').fetchone()[0]: raise ValueError("Target is not empty; refusing migration")
        verified={}
        for table,expected in manifest.items():
            columns=expected["columns"]
            if any(not re.fullmatch(r"[a-z_][a-z0-9_]*",column) for column in columns): raise ValueError("Unexpected column")
            selection=",".join('"'+column+'"' for column in columns)
            for row in src.execute('SELECT '+selection+' FROM "'+table+'"'):
                if table in {'memory_queue_guard','eval_queue_guard'}:
                    if tuple(row)!=(1,):raise ValueError("Unexpected legacy memory guard")
                    continue
                db.execute('INSERT INTO "'+table+'"('+selection+') VALUES('+",".join("?" for _ in columns)+")",row)
            received=row_digest([dict(r) for r in db.execute('SELECT '+selection+' FROM "'+table+'"')])
            if received!={k:expected[k] for k in ("count","sha256")}: raise RuntimeError("Migration content mismatch in "+table)
            verified[table]=received
        for table,column in (("messages","seq"),("events","seq"),("telemetry","seq"),("service_audit","seq")):
            sequence=db.execute("SELECT pg_get_serial_sequence(?,?)",(db.schema+"."+table,column)).fetchone()[0]
            maximum=db.execute('SELECT MAX("'+column+'") FROM "'+table+'"').fetchone()[0]
            db.execute("SELECT setval(?, ?, ?)",(sequence,maximum or 1,maximum is not None))
        # New bookkeeping is derived only after every legacy column is verified.
        db.execute("INSERT INTO knowledge_documents SELECT tenant,document_id,MIN(owner),MIN(visibility),MIN(version),1,? FROM chunks GROUP BY tenant,document_id ON CONFLICT(tenant,document_id) DO NOTHING",(time.time(),))
        for row in db.execute("SELECT * FROM knowledge_documents").fetchall():
            db.execute("INSERT INTO index_outbox VALUES(?,?,?,?,'pending',0,?,NULL,?)",(uuid.uuid4().hex,row["tenant"],row["document_id"],row["generation"],time.time(),time.time()))
        for row in db.execute("SELECT * FROM runs").fetchall():
            if db.execute("SELECT 1 FROM turn_receipts WHERE run_id=? LIMIT 1",(row["id"],)).fetchone(): continue
            seq=db.execute("SELECT MIN(seq) FROM messages WHERE run_id=? AND role='user'",(row["id"],)).fetchone()[0]
            # Legacy HTTP replies had no request keys. Do not invent past turn
            # identities; expose the preserved cutover state as legacy evidence.
            response=Store._snapshot(db,row)|{"legacy_cutover":True}
            db.execute("INSERT INTO turn_receipts VALUES(?,?,?,?,?,?,?,?,?,?)",(row["tenant"],row["owner"],row["id"],row["request_key"],digest(["create",row["message"]]),seq,row["status"],encode(response),row["created"],time.time()))
        return verified


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--apply",action="store_true")
    args=parser.parse_args()
    if not args.apply: parser.error("Use --apply after stopping the legacy app; target must be empty")
    source=validate_path(ROOT/"data"/"resolvedesk.sqlite3")
    # Refuse a live legacy task; caller must stop the old HTTP process before cutover.
    with sqlite3.connect(source.as_uri()+"?mode=ro",uri=True) as db:
        if db.execute("SELECT COUNT(*) FROM runs WHERE status IN ('running','queued')").fetchone()[0]: raise RuntimeError("Active legacy tasks must be paused before migration")
    directory=validate_path(ROOT/".codex-runtime"/"checkpoints"/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")+"-"+uuid.uuid4().hex[:8]))
    directory.mkdir(parents=True,exist_ok=False)
    backup=snapshot(source,directory/"resolvedesk.sqlite3",application=True)
    snapshot(ROOT/"data"/"billing"/"usage.sqlite3",directory/"usage.sqlite3")
    from resolvedesk.settings import ServiceSettings
    target=Store(ServiceSettings.load().database_url)
    try:
        initialize(target)
        report={"status":"verified","source_snapshot":str(backup.relative_to(ROOT)),"tables":copy_to_postgres(backup,target),
            "ledger":"original cumulative ledger preserved; independent consistent snapshot created","legacy_receipts":"pre-cutover reply keys unavailable; no invented historical turn keys"}
        path=directory/"migration-report.json"
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"status":report["status"],"tables":len(report["tables"]),"rows":sum(t["count"] for t in report["tables"].values()),"evidence":str(path.relative_to(ROOT))},ensure_ascii=False))
    finally: target.close()


if __name__=="__main__": main()
