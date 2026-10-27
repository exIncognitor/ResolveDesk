"""Explicit, tenant-scoped evidence retention; no background deletion.

Completed old runs may lose bulky traces/checkpoints and cached turn responses.
Request keys/hashes remain as tombstones so expiry cannot re-execute paid work.
Business records, messages, approvals, audit and financial ledger stay intact.
Caller previews first and takes a database backup before applying to real data.
"""
import time


class Retention:
    def __init__(self,store): self.store=store

    def sweep(self,p,*,days=30,apply=False):
        if p.role!="support": raise PermissionError("Support role required")
        if type(days)is not int or not 7<=days<=3650: raise ValueError("Retention must be 7-3650 days")
        cutoff=time.time()-days*86400
        counts={"events":0,"checkpoints":0,"receipt_responses":0,"index_jobs":0,"telemetry":0}
        with self.store.db(apply) as db:
            rows=db.execute(db.locked("SELECT id FROM runs WHERE tenant=? AND status='completed' AND updated<? ORDER BY updated LIMIT 500"),(p.tenant,cutoff)).fetchall()
            for row in rows:
                run_id=row["id"]
                for table,label,condition,params in (
                    ("events","events","run_id=? AND created<?",(run_id,cutoff)),
                    ("checkpoints","checkpoints","run_id=?",(run_id,)),
                    ("turn_receipts","receipt_responses","run_id=? AND updated<? AND response IS NOT NULL",(run_id,cutoff)),
                ):
                    counts[label]+=db.execute("SELECT COUNT(*) FROM "+table+" WHERE "+condition,params).fetchone()[0]
                    if apply:
                        if table=="turn_receipts": db.execute("UPDATE turn_receipts SET response=NULL,status='expired' WHERE "+condition,params)
                        else: db.execute("DELETE FROM "+table+" WHERE "+condition,params)
            for table,label,condition in (("index_outbox","index_jobs","tenant=? AND state='done' AND created<?"),("telemetry","telemetry","tenant=? AND created<?")):
                counts[label]=db.execute("SELECT COUNT(*) FROM "+table+" WHERE "+condition,(p.tenant,cutoff)).fetchone()[0]
                if apply: db.execute("DELETE FROM "+table+" WHERE "+condition,(p.tenant,cutoff))
        return {"applied":bool(apply),"days":days,"tenant":p.tenant,"counts":counts,"run_batch_limit":500,
            "preserved":["request key/hash tombstones","business states and audit","messages","financial ledger"]}
