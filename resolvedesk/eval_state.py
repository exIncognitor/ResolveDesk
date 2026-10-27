"""Durable evaluation units. An uncertain paid unit is never automatically repeated."""
import json

from resolvedesk.store import Conflict,encode

EVAL_STATE_TABLES={'eval_units','eval_snapshots'}


class UncertainUnit(Conflict):
    pass


class EvalState:
    def __init__(self,store,job_id,fence=None):
        self.store,self.job_id,self.fence=store,job_id,fence
        with store.database.schema_transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS eval_units(job_id TEXT,unit_key TEXT,state TEXT,payload TEXT,error TEXT,
                    updated REAL,PRIMARY KEY(job_id,unit_key));
                CREATE TABLE IF NOT EXISTS eval_snapshots(job_id TEXT PRIMARY KEY,payload TEXT,created REAL);
            ''')

    def check(self,db):
        if self.fence:self.fence(db)

    def result(self,key,value=None):
        """Memoize a pure report snapshot without starting a new execution unit."""
        with self.store.db(True) as db:
            self.check(db)
            if value is not None:
                raw=encode(value)
                if len(raw.encode())>2_000_000:raise ValueError('Evaluation result exceeds 2MB')
                db.execute("INSERT INTO eval_units VALUES(?,?,'done',?,NULL,?) ON CONFLICT(job_id,unit_key) DO NOTHING",(self.job_id,key,raw,db.now()))
            row=db.execute("SELECT payload FROM eval_units WHERE job_id=? AND unit_key=? AND state='done'",(self.job_id,key)).fetchone()
            return json.loads(row[0]) if row else None

    def snapshot(self,prepare):
        with self.store.db(True) as db:
            self.check(db)
            row=db.execute('SELECT payload FROM eval_snapshots WHERE job_id=?',(self.job_id,)).fetchone()
            if row:return json.loads(row[0])
            payload=prepare(db)
            raw=encode(payload)
            if len(raw.encode())>20_000_000:raise ValueError('Evaluation evidence snapshot exceeds 20MB')
            db.execute('INSERT INTO eval_snapshots VALUES(?,?,?)',(self.job_id,raw,db.now()))
            return payload

    async def unit(self,key,operation,recover=None):
        with self.store.db(True) as db:
            self.check(db)
            previous=db.execute('SELECT * FROM eval_units WHERE job_id=? AND unit_key=?',(self.job_id,key)).fetchone()
            if previous and previous['state']=='done':return json.loads(previous['payload'])
            if not previous:
                db.execute("INSERT INTO eval_units VALUES(?,?,'started',NULL,NULL,?)",(self.job_id,key,db.now()))
        if previous:
            # Recovery is observational only (e.g. a committed terminal receipt).
            value=recover() if recover and previous['state']=='started' else None
            if value is None:raise UncertainUnit('Unit is unavailable; automatic replay is disabled')
        else:
            try:value=await operation()
            except Exception as exc:
                with self.store.db(True) as db:
                    self.check(db)
                    db.execute("UPDATE eval_units SET state='failed',error=?,updated=? WHERE job_id=? AND unit_key=?",(type(exc).__name__,db.now(),self.job_id,key))
                raise
        raw=encode(value)
        if len(raw.encode())>2_000_000:raise ValueError('Evaluation unit result exceeds 2MB')
        with self.store.db(True) as db:
            self.check(db)
            db.execute("UPDATE eval_units SET state='done',payload=?,error=NULL,updated=? WHERE job_id=? AND unit_key=?",(raw,db.now(),self.job_id,key))
        return value
