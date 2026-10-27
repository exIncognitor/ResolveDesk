"""Explicit evaluation retirement with scoped resource ownership and retryable cleanup.

No timer deletes data. The operator previews a specific immutable plan and backs
up before apply. Tombstones survive, as do authored datasets and pinned baselines.
"""
import hashlib
import json
from pathlib import Path
import re
import uuid

import httpx

from resolvedesk.store import Conflict,digest,encode

EVAL_RETENTION_TABLES={'eval_retirements'}
ELIGIBLE={'completed','failed','paused','retiring','retire_failed'}


def resource_plan(manifest):
    if 'identity' not in manifest:return None  # Retrieval-only snapshot has no sandbox.
    identity=manifest['identity']
    if not re.fullmatch(r'eval_rd_[a-f0-9]{32}',identity):raise ValueError('Invalid evaluation sandbox identity')
    resources=manifest.get('resources')
    if not resources:raise ValueError('Legacy sandbox ownership must be registered before retirement')
    collection=identity.replace('_','-')
    if resources.get('schema')!=identity or resources.get('knowledge_collection')!=collection or resources.get('memory_collection')!='resolvedesk-memory-'+hashlib.sha256(collection.encode()).hexdigest()[:16]:raise ValueError('Unexpected evaluation resources')
    namespace=resources.get('redis_namespace')
    if manifest['backend']=='postgresql' and (not isinstance(namespace,str) or not re.fullmatch(r'[a-zA-Z0-9:_-]{1,200}',namespace) or not namespace.endswith(':'+identity)):raise ValueError('Unverifiable evaluation Redis namespace')
    return {**resources,'backend':manifest['backend'],'fingerprint':digest(manifest),'embedding_identity':manifest.get('embedding_identity')}


class EvalRetention:
    def __init__(self,store):
        self.store=store
        with store.database.schema_transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS eval_retirements(job_id TEXT PRIMARY KEY,plan TEXT,state TEXT,
                execution_owner TEXT,lease_until REAL,backup TEXT,error TEXT,updated REAL)''')

    def _plan(self,db,p,job_id):
        job=db.execute('SELECT * FROM eval_jobs WHERE id=? AND tenant=? AND owner=?',(job_id,p.tenant,p.user)).fetchone()
        if not job:raise KeyError('Evaluation not found')
        if job['status'] not in ELIGIBLE:raise Conflict('Evaluation is active or already retired')
        if db.execute('SELECT 1 FROM quality_baselines WHERE job_id=?',(job_id,)).fetchone():raise Conflict('Pinned evaluation baseline is protected')
        row=db.execute('SELECT payload FROM eval_snapshots WHERE job_id=?',(job_id,)).fetchone()
        manifest=json.loads(row[0]) if row else {}
        resources=resource_plan(manifest)
        if resources and resources['backend']!='postgresql':raise ValueError('Offline fixture is preserved; requires a separate verified file archive')
        return {'job_id':job_id,'tenant':p.tenant,'owner':p.user,'source_schema':self.store.database.schema,'resources':resources,
            'result_fingerprint':digest(job['result']),'options_fingerprint':digest(job['options']),
            'manifest_fingerprint':digest(manifest),'units':db.execute('SELECT COUNT(*) FROM eval_units WHERE job_id=?',(job_id,)).fetchone()[0]}

    def preview(self,p,*,days=30,job_id=None):
        if p.role!='support':raise PermissionError()
        if type(days)is not int or not 7<=days<=3650:raise ValueError('Retention must be 7-3650 days')
        with self.store.db() as db:
            if job_id:ids=[job_id]
            else:ids=[r[0] for r in db.execute("SELECT id FROM eval_jobs WHERE tenant=? AND owner=? AND created<? AND status IN ('completed','failed','paused','retiring','retire_failed') ORDER BY created,id LIMIT 20",(p.tenant,p.user,db.now()-days*86400))]
            plans,protected=[],[]
            for identity in ids:
                try:
                    plan=self._plan(db,p,identity)
                    plans.append(plan|{'confirmation':digest(plan)})
                except (Conflict,ValueError) as exc:protected.append({'job_id':identity,'reason':str(exc)})
        return {'applied':False,'days':days,'plans':plans,'protected':protected,'batch_limit':20,
            'preserved':['authored datasets','pinned baselines','evaluation and unit tombstones','source business data','financial ledger']}

    def apply(self,p,job_id,confirmation,cleaner,archive):
        if p.role!='support':raise PermissionError()
        owner=uuid.uuid4().hex
        with self.store.db(True) as db:
            # Same order as admission and baseline selection; no pinned/retire race.
            db.execute(db.locked('SELECT id FROM eval_queue_guard WHERE id=1')).fetchone()
            job=db.execute(db.locked('SELECT id FROM eval_jobs WHERE id=? AND tenant=? AND owner=?'),(job_id,p.tenant,p.user)).fetchone()
            if not job:raise KeyError('Evaluation not found')
            plan=self._plan(db,p,job_id)
            if digest(plan)!=confirmation:raise Conflict('Retirement plan changed; preview again')
            previous=db.execute('SELECT * FROM eval_retirements WHERE job_id=?',(job_id,)).fetchone()
            if previous and previous['state']=='running' and previous['lease_until']>db.now():raise Conflict('Retirement is already owned')
            # Backup implementation verifies an independent copy before deletion.
            backup=archive.verify(plan)
            db.execute("INSERT INTO eval_retirements VALUES(?,?,'running',?,?,?,NULL,?) ON CONFLICT(job_id) DO UPDATE SET state='running',execution_owner=excluded.execution_owner,lease_until=excluded.lease_until,backup=excluded.backup,error=NULL,updated=excluded.updated",
                (job_id,encode(plan),owner,db.now()+300,encode(backup),db.now()))
            db.execute("UPDATE eval_jobs SET status='retiring',execution_owner=NULL,lease_until=0 WHERE id=?",(job_id,))
        try:
            # A per-job row lock serializes retries. Schema drops use no CASCADE,
            # so external objects can never be deleted as collateral dependencies.
            with self.store.db(True) as db:
                db.execute(db.locked('SELECT id FROM eval_jobs WHERE id=?'),(job_id,)).fetchone()
                row=db.execute(db.locked('SELECT * FROM eval_retirements WHERE job_id=?'),(job_id,)).fetchone()
                if row['execution_owner']!=owner or row['state']!='running' or row['lease_until']<=db.now():raise Conflict('Retirement lease expired')
                if plan['resources']:cleaner.remove(db,plan['resources'])
                options=json.loads(db.execute('SELECT options FROM eval_jobs WHERE id=?',(job_id,)).fetchone()[0])
                tombstone={k:options[k] for k in ('kind','suite_id') if k in options}
                tombstone['retired_options_fingerprint']=plan['options_fingerprint']
                db.execute("UPDATE eval_units SET state='expired',payload=NULL,error='retired' WHERE job_id=?",(job_id,))
                db.execute('UPDATE eval_snapshots SET payload=? WHERE job_id=?',(encode({'retired':True,'manifest_fingerprint':plan['manifest_fingerprint'],'resources':plan['resources']}),job_id))
                db.execute("UPDATE eval_jobs SET status='expired',result=NULL,options=?,error='explicitly_retired' WHERE id=?",(encode(tombstone),job_id))
                db.execute("UPDATE eval_retirements SET state='completed',execution_owner=NULL,lease_until=0,error=NULL,updated=? WHERE job_id=?",(db.now(),job_id))
        except Exception as exc:
            with self.store.db(True) as db:
                row=db.execute(db.locked('SELECT execution_owner,state FROM eval_retirements WHERE job_id=?'),(job_id,)).fetchone()
                if row and row['execution_owner']==owner and row['state']=='running':
                    db.execute("UPDATE eval_retirements SET state='failed',execution_owner=NULL,lease_until=0,error=?,updated=? WHERE job_id=?",(type(exc).__name__,db.now(),job_id))
                    db.execute("UPDATE eval_jobs SET status='retire_failed',error='retirement_failed' WHERE id=?",(job_id,))
            raise
        return {'applied':True,'job_id':job_id,'status':'expired','backup':backup,'resources_removed':bool(plan['resources'])}


class ServiceResourceCleaner:
    def __init__(self,redis_url,chroma_url):
        import redis
        self.redis=redis.Redis.from_url(redis_url,decode_responses=True,socket_connect_timeout=3,socket_timeout=3)
        self.http=httpx.Client(base_url=chroma_url,timeout=httpx.Timeout(5,connect=2),trust_env=False)
        self.base='/api/v2/tenants/default_tenant/databases/default_database/collections'

    def remove(self,db,resources):
        if resources['backend']!='postgresql' or db.backend!='postgresql':raise ValueError('Use archived offline fixtures; service cleanup requires PostgreSQL')
        schema=resources['schema']
        if schema==db.schema:raise ValueError('Cannot retire the source application schema')
        exists=db.execute('SELECT 1 FROM pg_namespace WHERE nspname=?',(schema,)).fetchone()
        if exists:
            marker=db.execute('SELECT fingerprint FROM "'+schema+'".eval_sandbox_meta WHERE id=1').fetchone()
            if marker is None or marker[0]!=resources['fingerprint']:raise ValueError('Sandbox ownership marker mismatch')
            if db.execute('SELECT COUNT(*) FROM "'+schema+'".runs WHERE status IN (\'running\',\'queued\')').fetchone()[0]:raise Conflict('Sandbox still has active runs')
            if db.execute('SELECT 1 FROM pg_tables WHERE schemaname=? AND tablename=\'memory_users\'',(schema,)).fetchone():
                if db.execute('SELECT 1 FROM "'+schema+'".memory_users WHERE state=\'running\' AND lease_until>?',(db.now(),)).fetchone():raise Conflict('Sandbox memory worker is still leased')
        tables=[r[0] for r in db.execute('SELECT tablename FROM pg_tables WHERE schemaname=?',(schema,))]
        from resolvedesk.schema_tables import APPLICATION_TABLES
        if not set(tables)<=APPLICATION_TABLES|{'eval_sandbox_meta'}:raise ValueError('Unexpected sandbox tables')
        names=[resources['knowledge_collection'],resources['memory_collection']]
        # Complete all read-only ownership checks before the first deletion.
        for name in names:
            response=self.http.get(self.base+'/'+name)
            if response.status_code!=404:
                response.raise_for_status()
                if response.json().get('metadata',{}).get('embedding_identity')!=resources['embedding_identity']:raise ValueError('Vector collection ownership mismatch')
        namespace=resources['redis_namespace']+':'
        keys=[]
        for key in self.redis.scan_iter(match=namespace+'*',count=100):
            if not key.startswith(namespace) or len(keys)>=5000:raise ValueError('Unexpected sandbox Redis key set')
            keys.append(key)
        # Execute transactional SQL dependency checks before remote deletion.
        # Unknown views/FKs/functions cause a rollback, without any CASCADE.
        for table in sorted(tables):
            if not re.fullmatch('[a-z_][a-z0-9_]*',table):raise ValueError('Invalid sandbox table name')
            db.execute('DROP TABLE "'+schema+'"."'+table+'"')
        if exists:db.execute('DROP SCHEMA "'+schema+'"')
        for name in names:
            response=self.http.delete(self.base+'/'+name)
            if response.status_code!=404:response.raise_for_status()
        if keys:self.redis.delete(*keys)

    def close(self):self.redis.close();self.http.close()


class VerifiedArchive:
    """Receipt of an independently created and checked full PostgreSQL backup."""
    def __init__(self,path,sha256,schemas):
        self.path,self.sha256,self.schemas=Path(path).absolute(),sha256,set(schemas)

    def verify(self,plan):
        for path in (self.path,*self.path.parents):
            if path.is_symlink() or path.is_junction():raise ValueError('Unexpected backup link')
        if not self.path.is_file() or self.path.stat().st_size<100 or hashlib.sha256(self.path.read_bytes()).hexdigest()!=self.sha256:raise ValueError('Backup verification failed')
        schema=(plan.get('resources') or {}).get('schema')
        if plan.get('source_schema') and plan['source_schema'] not in self.schemas:raise ValueError('Backup does not cover the source application schema')
        if schema and schema not in self.schemas:raise ValueError('Backup does not cover the selected sandbox')
        return {'path':str(self.path),'sha256':self.sha256}
