"""Transactional execution samples and cumulative, cross-worker histograms."""
import json
import math
import uuid

BUCKETS=(.01,.05,.1,.25,.5,1.,2.,3.,5.,10.,20.,30.,50.)
METRIC_TABLES={'metric_totals','execution_attempts','monitor_tool_health'}


def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS telemetry(seq INTEGER PRIMARY KEY AUTOINCREMENT,tenant TEXT,kind TEXT,
        name TEXT,success INTEGER,seconds REAL,created REAL)''')
    for column,declaration in (
        ('owner',"TEXT NOT NULL DEFAULT ''"),('instance',"TEXT NOT NULL DEFAULT 'legacy'"),
        ('status',"TEXT NOT NULL DEFAULT ''"),('event_key','TEXT'),('run_id','TEXT'),('turn_key','TEXT'),
        ('detail',"TEXT NOT NULL DEFAULT '{}'"),('measured','INTEGER NOT NULL DEFAULT 1')):
        if not db.has_column('telemetry',column):db.execute('ALTER TABLE telemetry ADD COLUMN '+column+' '+declaration)
    db.executescript('''
        CREATE INDEX IF NOT EXISTS telemetry_window ON telemetry(tenant,created);
        CREATE TABLE IF NOT EXISTS metric_totals(tenant TEXT,kind TEXT,name TEXT,instance TEXT,status TEXT,
            total BIGINT,measured BIGINT,seconds REAL,buckets TEXT,PRIMARY KEY(tenant,kind,name,instance,status));
        CREATE TABLE IF NOT EXISTS execution_attempts(run_id TEXT,epoch INTEGER,tenant TEXT,owner TEXT,turn_key TEXT,
            turn_seq INTEGER,instance TEXT,started REAL,lease_until REAL,finished REAL,status TEXT,error TEXT,
            PRIMARY KEY(run_id,epoch));
        CREATE TABLE IF NOT EXISTS monitor_tool_health(tenant TEXT,worker TEXT,name TEXT,payload TEXT,seen REAL,
            PRIMARY KEY(tenant,worker,name));
    ''')
    if not db.has_column('execution_attempts','projected'):db.execute('ALTER TABLE execution_attempts ADD COLUMN projected INTEGER NOT NULL DEFAULT 0')
    # One-time raw-sample projection; no invented old turn/instance attribution.
    if not db.has_column('telemetry','projected'):
        db.execute('ALTER TABLE telemetry ADD COLUMN projected INTEGER NOT NULL DEFAULT 0')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS telemetry_event_identity ON telemetry(tenant,event_key)')
    for row in db.execute('SELECT * FROM telemetry WHERE projected=0 ORDER BY seq').fetchall():
        values=dict(row);values['status']=values['status'] or ('success' if values['success'] else 'failure')
        _accumulate(db,values)
        db.execute('UPDATE telemetry SET status=?,projected=1 WHERE seq=?',(values['status'],values['seq']))
    project_attempts(db)


def _accumulate(db,sample):
    key=tuple(sample[c] for c in ('tenant','kind','name','instance','status'))
    db.execute("INSERT INTO metric_totals VALUES(?,?,?,?,?,0,0,0,'[]') ON CONFLICT(tenant,kind,name,instance,status) DO NOTHING",key)
    row=db.execute(db.locked('SELECT * FROM metric_totals WHERE tenant=? AND kind=? AND name=? AND instance=? AND status=?'),key).fetchone()
    counts=json.loads(row['buckets']) or [0]*len(BUCKETS)
    seconds=sample['seconds']
    if sample['measured']:
        counts=[n+int(seconds<=bound) for n,bound in zip(counts,BUCKETS,strict=True)]
    db.execute('UPDATE metric_totals SET total=total+1,measured=measured+?,seconds=seconds+?,buckets=? WHERE tenant=? AND kind=? AND name=? AND instance=? AND status=?',
        (sample['measured'],seconds if sample['measured'] else 0,encoded(counts),*key))


def write_sample(db,tenant,kind,name,success,seconds=0,*,owner='',instance='legacy',status=None,event_key=None,run_id=None,turn_key=None,detail=None):
    measured=seconds is not None
    if measured and (not math.isfinite(seconds) or seconds<0):raise ValueError('Invalid measured latency')
    status=status or ('success' if success else 'failure')
    status={'failed':'failure','completed':'success','paused':'cancelled'}.get(status,status)
    if status not in {'success','failure','partial','cancelled','awaiting_input','awaiting_confirmation','interrupted','degraded'}:raise ValueError('Invalid execution outcome')
    success=bool(success) and status not in {'failure','partial','cancelled','interrupted','degraded'}
    sample={'tenant':tenant,'kind':kind,'name':name,'success':int(success),'seconds':seconds or 0,'created':db.now(),
        'owner':owner,'instance':instance,'status':status,'event_key':event_key or uuid.uuid4().hex,'run_id':run_id,'turn_key':turn_key,
        'detail':encoded(detail or {}),'measured':int(measured),'projected':1}
    columns=list(sample)
    inserted=db.execute('INSERT INTO telemetry('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+') ON CONFLICT(tenant,event_key) DO NOTHING',tuple(sample[c] for c in columns))
    if inserted.rowcount:_accumulate(db,sample)
    return bool(inserted.rowcount)


def finish_attempt(db,row):
    """Freeze the attempt outcome in the same transaction as the terminal receipt."""
    if row['status'] in {'queued','running'}:return
    attempt=db.execute(db.locked('SELECT * FROM execution_attempts WHERE run_id=? AND epoch=?'),(row['id'],row['execution_epoch'])).fetchone()
    if not attempt or attempt['finished'] is not None:return
    status=row['status']
    interrupted=row['error'] in {'execution_interrupted','process_interrupted','lease_lost'}
    outcome='interrupted' if interrupted else 'partial' if row['error']=='specialist_partial' else status
    now=db.now();seconds=None if interrupted else max(0,now-attempt['started'])
    db.execute('UPDATE execution_attempts SET finished=?,status=?,error=? WHERE run_id=? AND epoch=?',
        (now,outcome,row['error'],row['id'],row['execution_epoch']))
    # The durable execution fact survives an outage of the derived counters.
    previous_lock=db.execute("SELECT current_setting('lock_timeout')").fetchone()[0] if db.backend=='postgresql' else None
    db.execute('SAVEPOINT main_metric_projection')
    try:
        if previous_lock is not None:db.execute("SELECT set_config('lock_timeout','100ms',true)")
        current=db.execute('SELECT * FROM execution_attempts WHERE run_id=? AND epoch=?',(row['id'],row['execution_epoch'])).fetchone()
        _project_attempt(db,current)
    except Exception as exc:
        db.execute('ROLLBACK TO SAVEPOINT main_metric_projection')
        db.execute("INSERT INTO events(run_id,kind,payload,created) VALUES(?,'monitor_error',?,?)",(row['id'],encoded({'type':type(exc).__name__,'pending_projection':True}),now))
    finally:
        db.execute('RELEASE SAVEPOINT main_metric_projection')
        if previous_lock is not None:db.execute("SELECT set_config('lock_timeout',?,true)",(previous_lock,))
    db.execute("INSERT INTO events(run_id,kind,payload,created) VALUES(?,'duration',?,?)",(row['id'],encoded({'seconds':seconds,'epoch':row['execution_epoch'],'turn_key':attempt['turn_key'],'status':outcome}),now))


def _project_attempt(db,attempt):
    seconds=None if attempt['status']=='interrupted' else max(0,attempt['finished']-attempt['started'])
    write_sample(db,attempt['tenant'],'run','desk',attempt['status'] in {'completed','awaiting_input','awaiting_confirmation'} and not attempt['error'],seconds,
        owner=attempt['owner'],instance=attempt['instance'],status=attempt['status'],event_key=f'run:{attempt["run_id"]}:{attempt["epoch"]}',run_id=attempt['run_id'],turn_key=attempt['turn_key'],
        detail={'epoch':attempt['epoch'],'turn_seq':attempt['turn_seq'],'error_code':attempt['error'],'duration_source':'database_terminal_transaction' if seconds is not None else 'unknown_after_interruption'})
    db.execute('UPDATE execution_attempts SET projected=1 WHERE run_id=? AND epoch=?',(attempt['run_id'],attempt['epoch']))


def project_attempts(db,tenant=None,limit=500):
    scope,params=(' AND tenant=?',[tenant]) if tenant is not None else ('',[])
    sql='SELECT * FROM execution_attempts WHERE finished IS NOT NULL AND projected=0'+scope+' ORDER BY finished LIMIT ?'
    if db.backend=='postgresql':sql+=' FOR UPDATE SKIP LOCKED'
    for attempt in db.execute(sql,[*params,limit]).fetchall():_project_attempt(db,attempt)
