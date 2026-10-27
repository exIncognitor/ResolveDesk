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
    # One-time raw-sample projection; no invented old turn/instance attribution.
    if not db.has_column('telemetry','projected'):
        db.execute('ALTER TABLE telemetry ADD COLUMN projected INTEGER NOT NULL DEFAULT 0')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS telemetry_event_identity ON telemetry(tenant,event_key)')
    for row in db.execute('SELECT * FROM telemetry WHERE projected=0 ORDER BY seq').fetchall():
        values=dict(row);values['status']=values['status'] or ('success' if values['success'] else 'failure')
        _accumulate(db,values)
        db.execute('UPDATE telemetry SET status=?,projected=1 WHERE seq=?',(values['status'],values['seq']))


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
    write_sample(db,row['tenant'],'run','desk',status in {'completed','awaiting_input','awaiting_confirmation'} and not row['error'],seconds,
        owner=row['owner'],instance=attempt['instance'],status=outcome,event_key=f'run:{row["id"]}:{row["execution_epoch"]}',run_id=row['id'],turn_key=attempt['turn_key'],
        detail={'epoch':row['execution_epoch'],'turn_seq':attempt['turn_seq'],'error_code':row['error'],'duration_source':'database_terminal_transaction' if seconds is not None else 'unknown_after_interruption'})
    db.execute("INSERT INTO events(run_id,kind,payload,created) VALUES(?,'duration',?,?)",(row['id'],encoded({'seconds':seconds,'epoch':row['execution_epoch'],'turn_key':attempt['turn_key'],'status':outcome}),now))
