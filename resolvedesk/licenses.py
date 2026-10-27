"""Transactional simulated named-user seats; no vendor or payment connection.

BusinessService owns the case lock and idempotent action receipt. Reservation
and allocation then lock one product pool, so competing cases cannot oversell.
Expiry never extends a lease and is safe before or after background sweeping.
"""
from resolvedesk.store import Conflict

PRODUCT='IntelliJ IDEA Ultimate'
INITIAL_SEATS=2
RESERVATION_SECONDS=900
LICENSE_TABLES={'license_pools','license_allocations'}


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS license_pools(tenant TEXT,product TEXT,total INTEGER NOT NULL,
            PRIMARY KEY(tenant,product),CHECK(total>=0));
        CREATE TABLE IF NOT EXISTS license_allocations(ticket_id TEXT PRIMARY KEY,tenant TEXT,product TEXT,
            owner TEXT,state TEXT,expires REAL,updated REAL);
        CREATE INDEX IF NOT EXISTS license_occupancy ON license_allocations(tenant,product,state,expires);
    ''')


def validate(fields):
    if fields.get('category')!='software':raise ValueError('软件授权申请必须选择 software 类别')
    missing=[]
    if not fields.get('product'):missing.append('软件产品与版本')
    elif fields['product']!=PRODUCT:raise ValueError('产品不在模拟商业席位目录')
    days=fields.get('duration_days',0)
    if not days:missing.append('使用期限天数')
    elif type(days)is not int or not 1<=days<=30:raise ValueError('软件使用期限为 1 至 30 天')
    if not str(fields.get('description','')).strip():missing.append('业务用途')
    return missing


def catalog(db,p):
    now=db.now()
    pool=db.execute('SELECT total FROM license_pools WHERE tenant=? AND product=?',(p.tenant,PRODUCT)).fetchone()
    total=pool['total'] if pool else INITIAL_SEATS
    counts={r['state']:r['n'] for r in db.execute("SELECT state,COUNT(*) AS n FROM license_allocations WHERE tenant=? AND product=? AND state IN ('reserved','allocated') AND expires>? GROUP BY state",(p.tenant,PRODUCT,now))}
    mine=[dict(r) for r in db.execute("SELECT ticket_id,product,state,expires FROM license_allocations WHERE tenant=? AND owner=? AND state IN ('reserved','allocated') AND expires>? ORDER BY ticket_id",(p.tenant,p.user,now))]
    return {'product':PRODUCT,'total_seats':total,'reserved_seats':counts.get('reserved',0),'allocated_seats':counts.get('allocated',0),
        'available_seats':max(0,total-sum(counts.values())),'approval':'其他IT操作员审批后预占、再分配模拟席位',
        'personal_purchase_reimbursement':False,'reservation_seconds':RESERVATION_SECONDS,'my_allocations':mine,'simulation':True}


def allowed(p,row):
    state=row['status'];actions=[]
    if row['owner']==p.user:
        if state in {'pending_approval','approved','reserved'}:actions.append('cancel')
        if state=='allocated':actions.append('revoke')
    if p.role=='support':
        if row['owner']!=p.user:
            actions+={'pending_approval':['approve','reject'],'approved':['reserve'],'reserved':['allocate']}.get(state,[])
        if state=='allocated' and 'revoke' not in actions:actions.append('revoke')
    return actions


def apply(db,p,row,fields,action):
    """Caller holds case lock; seats are held only by unexpired slot records."""
    now=db.now()
    if action in {'reserve','allocate'}:
        db.execute('INSERT INTO license_pools VALUES(?,?,?) ON CONFLICT(tenant,product) DO NOTHING',(p.tenant,PRODUCT,INITIAL_SEATS))
        pool=db.execute(db.locked('SELECT total FROM license_pools WHERE tenant=? AND product=?'),(p.tenant,PRODUCT)).fetchone()
        now=db.now()  # A pool-lock wait must not make an expired lease allocatable.
        if action=='reserve':
            used=db.execute("SELECT COUNT(*) FROM license_allocations WHERE tenant=? AND product=? AND state IN ('reserved','allocated') AND expires>?",(p.tenant,PRODUCT,now)).fetchone()[0]
            if used>=pool['total']:raise Conflict('模拟席位不足；保留已批准状态，等待回收或人工采购审核')
            own=db.execute("SELECT 1 FROM license_allocations WHERE tenant=? AND product=? AND owner=? AND state IN ('reserved','allocated') AND expires>?",(p.tenant,PRODUCT,row['owner'],now)).fetchone()
            if own:raise Conflict('申请人已持有该产品的有效预占或分配')
            db.execute("INSERT INTO license_allocations VALUES(?,?,?,?,'reserved',?,?)",(row['ticket_id'],p.tenant,PRODUCT,row['owner'],now+RESERVATION_SECONDS,now))
        else:
            slot=db.execute("SELECT * FROM license_allocations WHERE ticket_id=? AND tenant=?",(row['ticket_id'],p.tenant)).fetchone()
            if not slot or slot['state']!='reserved' or slot['expires']<=now:raise Conflict('预占已过期，请刷新申请状态')
            if validate(fields):raise ValueError('软件授权申请不完整')
            db.execute("UPDATE license_allocations SET state='allocated',expires=?,updated=? WHERE ticket_id=?",(now+fields['duration_days']*86400,now,row['ticket_id']))
    elif action in {'revoke','cancel'}:
        db.execute("UPDATE license_allocations SET state=?,expires=?,updated=? WHERE ticket_id=? AND tenant=? AND state IN ('reserved','allocated')",('revoked' if action=='revoke' else 'cancelled',now,now,row['ticket_id'],p.tenant))


def expire_case(db,row):
    """Caller holds one case lock. Return current row; never change other cases."""
    if row['kind']!='license_request' or row['status'] not in {'reserved','allocated'}:return row
    now=db.now()
    slot=db.execute('SELECT state,expires FROM license_allocations WHERE ticket_id=? AND tenant=?',(row['ticket_id'],row['tenant'])).fetchone()
    if slot and slot['state'] in {'reserved','allocated'} and slot['expires']<=now:
        db.execute("UPDATE license_allocations SET state='expired',updated=? WHERE ticket_id=?",(now,row['ticket_id']))
        db.execute("UPDATE service_cases SET status='expired',version=version+1,updated=? WHERE ticket_id=?",(now,row['ticket_id']))
        db.execute("UPDATE tickets SET status='expired' WHERE id=? AND tenant=?",(row['ticket_id'],row['tenant']))
        db.execute("INSERT INTO service_audit(ticket_id,tenant,actor,action,from_status,to_status,note,created) VALUES(?,?,?,'expire',?,'expired',?,?)",(row['ticket_id'],row['tenant'],'system:expiry',row['status'],'模拟席位租约到期回收',now))
        return db.execute('SELECT * FROM service_cases WHERE ticket_id=?',(row['ticket_id'],)).fetchone()
    return row


def expire_due(store,limit=100):
    """One case per transaction avoids case/pool lock inversions across batches."""
    count=0
    for _ in range(limit):
        with store.db(True) as db:
            statement="SELECT c.* FROM service_cases c JOIN license_allocations a ON a.ticket_id=c.ticket_id WHERE c.kind='license_request' AND c.status IN ('reserved','allocated') AND a.state IN ('reserved','allocated') AND a.expires<=? ORDER BY c.ticket_id LIMIT 1"
            if db.backend=='postgresql':statement+=' FOR UPDATE OF c SKIP LOCKED'
            row=db.execute(statement,(db.now(),)).fetchone()
            if row is None:break
            expire_case(db,row);count+=1
    return count
