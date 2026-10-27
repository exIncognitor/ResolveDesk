"""Immutable user-scoped evaluation datasets; authored data is never auto-pruned."""
import json
import time
import uuid

from resolvedesk.eval_models import EvalDataset
from resolvedesk.store import digest,encode

EVAL_DATA_TABLES={'eval_suites'}


class EvalDatasets:
    def __init__(self,store):
        self.store=store
        with store.database.schema_transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS eval_suites(id TEXT PRIMARY KEY,tenant TEXT,owner TEXT,title TEXT,
                fingerprint TEXT,payload TEXT,created REAL)''')

    def create(self,p,body):
        if p.role!='support':raise PermissionError()
        data=EvalDataset.model_validate(body).model_dump()
        with self.store.db(True) as db:
            if db.backend=='postgresql':db.execute('SELECT pg_advisory_xact_lock(?)',(721549288,))
            if db.execute('SELECT COUNT(*) FROM eval_suites WHERE tenant=? AND owner=?',(p.tenant,p.user)).fetchone()[0]>=50:raise ValueError('Evaluation dataset capacity reached')
            identity=uuid.uuid4().hex
            db.execute('INSERT INTO eval_suites VALUES(?,?,?,?,?,?,?)',(identity,p.tenant,p.user,data['title'],digest(data),encode(data),time.time()))
        return self.get(p,identity)

    def get(self,p,identity):
        if p.role!='support':raise PermissionError()
        with self.store.db() as db:
            row=db.execute('SELECT * FROM eval_suites WHERE id=? AND tenant=? AND owner=?',(identity,p.tenant,p.user)).fetchone()
            if row is None:raise KeyError('Dataset not found')
            return {**dict(row),'payload':json.loads(row['payload'])}

    def list(self,p):
        if p.role!='support':raise PermissionError()
        with self.store.db() as db:
            return [dict(row) for row in db.execute('SELECT id,title,fingerprint,created FROM eval_suites WHERE tenant=? AND owner=? ORDER BY created DESC',(p.tenant,p.user))]
