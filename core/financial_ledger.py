"""Historical development ledger, retained for evidence and legacy utilities.

Reuse the conservative reservation/uncertainty contract. A database advisory
transaction lock serializes the budget check and reservation across processes.
Runtime never creates an empty financial authority or silently falls back to a
local ledger. An operator must import a verified legacy snapshot first.
"""
from contextlib import contextmanager
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from core.paid_api import CostLedger,PaidCallBlocked
from resolvedesk.database import Database


def manifest(rows):
    values=sorted(json.dumps(dict(row),ensure_ascii=False,sort_keys=True,separators=(',',':')) for row in rows)
    return {'count':len(values),'sha256':hashlib.sha256('\n'.join(values).encode()).hexdigest()}


class PostgresCostLedger(CostLedger):
    def __init__(self,url,policy,*,schema='resolvedesk_finance'):
        self.policy=policy
        self.database=Database(url,schema=schema)
        if self.database.backend!='postgresql':
            self.database.close()
            raise PaidCallBlocked('Financial authority requires PostgreSQL')
        self.lock=int.from_bytes(hashlib.sha256(('resolvedesk-budget:'+schema).encode()).digest()[:8],'big',signed=True)

    @contextmanager
    def _transaction(self):
        with self.database.connect(True) as db:
            db.execute('SELECT pg_advisory_xact_lock(?)',(self.lock,))
            found=db.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=? AND table_name='finance_authority'",(db.schema,)).fetchone()
            if not found or not db.execute("SELECT 1 FROM finance_authority WHERE id='main' AND state='ready'").fetchone():
                raise PaidCallBlocked('Financial migration required; no paid request was sent')
            yield db

    def import_snapshot(self,path):
        path=Path(path).absolute()
        for item in (path,*path.parents):
            if item.is_symlink() or item.is_junction():raise ValueError('Financial snapshot must not follow links')
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as source:
            source.row_factory=sqlite3.Row
            if source.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Financial source integrity failed')
            rows=[dict(row) for row in source.execute('SELECT * FROM paid_calls ORDER BY id')]
        expected=manifest(rows)
        with self.database.schema_transaction() as db:
            db.execute('SELECT pg_advisory_xact_lock(?)',(self.lock,))
            db.executescript('''
                CREATE TABLE IF NOT EXISTS paid_calls(id TEXT PRIMARY KEY,status TEXT NOT NULL,reserved BIGINT NOT NULL,
                    charged BIGINT,input_tokens BIGINT,output_tokens BIGINT,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS finance_authority(id TEXT PRIMARY KEY,state TEXT NOT NULL,source_rows BIGINT,
                    source_sha256 TEXT,source_commitment BIGINT,migrated_at TEXT);
            ''')
            if db.execute('SELECT 1 FROM paid_calls LIMIT 1').fetchone() or db.execute('SELECT 1 FROM finance_authority LIMIT 1').fetchone():
                raise PaidCallBlocked('Financial target is not empty; refusing to reset or reimport')
            columns=('id','status','reserved','charged','input_tokens','output_tokens','created_at')
            for row in rows:
                if set(row)!=set(columns):raise RuntimeError('Unexpected financial source columns')
                if row['status'] not in {'reserved','uncertain','settled','overrun'} or row['reserved']<0 or (row['charged'] is not None and row['charged']<0):
                    raise RuntimeError('Invalid financial source row')
                db.execute('INSERT INTO paid_calls VALUES(?,?,?,?,?,?,?)',[row[c] for c in columns])
            received=manifest([dict(row) for row in db.execute('SELECT * FROM paid_calls')])
            if received!=expected:raise RuntimeError('Financial content verification failed')
            committed=int(self._committed(db))
            db.execute("INSERT INTO finance_authority VALUES('main','ready',?,?,?,?)",(expected['count'],expected['sha256'],committed,datetime.now(timezone.utc).isoformat()))
        return expected|{'commitment_micro_cny':committed}

    def close(self):self.database.close()
