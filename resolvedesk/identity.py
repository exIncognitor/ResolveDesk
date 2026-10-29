"""Database identities, expiring/revocable sessions and explicit local demo access."""
import hashlib
import hmac
import json
import os
import re
import secrets

from resolvedesk.store import Principal, encode

IDENTITY_TABLES = {'desk_accounts', 'desk_sessions', 'login_attempts', 'security_audit'}
NAME = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')


def password_hash(password, salt=None):
    if not isinstance(password, str) or not 10 <= len(password) <= 256:
        raise ValueError('密码需为10至256个字符')
    salt = salt or secrets.token_hex(16)
    result = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()
    return f'pbkdf2_sha256$600000${salt}${result}'


def password_matches(password, stored):
    try:
        method, rounds, salt, expected = stored.split('$')
        if method != 'pbkdf2_sha256' or rounds != '600000' or not isinstance(password, str) or len(password) > 256:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def demo_enabled():
    return os.getenv('RESOLVEDESK_DEMO_LOGIN_ENABLED', '').lower() == 'true' and os.getenv('RESOLVEDESK_ENV', 'development') != 'production'


class IdentityService:
    def __init__(self, store, *, session_seconds=28800):
        self.store, self.session_seconds = store, session_seconds
        with store.database.schema_transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS desk_accounts(tenant TEXT,user_id TEXT,display_name TEXT,
                    role TEXT,password_hash TEXT,enabled INTEGER NOT NULL,epoch INTEGER NOT NULL,
                    created REAL,updated REAL,PRIMARY KEY(tenant,user_id));
                CREATE TABLE IF NOT EXISTS desk_sessions(token_hash TEXT PRIMARY KEY,tenant TEXT,user_id TEXT,
                    epoch INTEGER,expires REAL,revoked INTEGER NOT NULL,kind TEXT,created REAL);
                CREATE INDEX IF NOT EXISTS session_actor ON desk_sessions(tenant,user_id,expires);
                CREATE TABLE IF NOT EXISTS login_attempts(id TEXT PRIMARY KEY,failures INTEGER,window_start REAL,blocked_until REAL);
                CREATE TABLE IF NOT EXISTS security_audit(id TEXT PRIMARY KEY,tenant TEXT,actor TEXT,
                    action TEXT,target TEXT,detail TEXT,created REAL);
            ''')

    @staticmethod
    def audit(db, p, action, target, detail=None):
        db.execute('INSERT INTO security_audit VALUES(?,?,?,?,?,?,?)',
                   (secrets.token_hex(16), p.tenant, p.user, action, target, encode(detail or {}), db.now()))

    def create_account(self, p, user, password, role='user', display_name=''):
        if p.role != 'support': raise PermissionError()
        if not NAME.fullmatch(user) or role not in {'user','support'} or not NAME.fullmatch(p.tenant):
            raise ValueError('无效账号或角色')
        hashed = password_hash(password)
        with self.store.db(True) as db:
            if db.execute('SELECT 1 FROM desk_accounts WHERE tenant=? AND user_id=?',(p.tenant,user)).fetchone():
                raise ValueError('账号已存在')
            now = db.now()
            db.execute('INSERT INTO desk_accounts VALUES(?,?,?,?,?,1,1,?,?)',
                       (p.tenant,user,(display_name or user)[:100],role,hashed,now,now))
            self.audit(db,p,'account.create',user,{'role':role})

    def member(self, tenant, user):
        with self.store.db() as db:
            row = db.execute('SELECT tenant,user_id,display_name,role,enabled FROM desk_accounts WHERE tenant=? AND user_id=?',(tenant,user)).fetchone()
            return dict(row) if row else None

    def members(self, p):
        if p.role != 'support': raise PermissionError()
        with self.store.db() as db:
            return [dict(row) for row in db.execute('SELECT user_id,display_name,role,enabled,created,updated FROM desk_accounts WHERE tenant=? ORDER BY user_id LIMIT 500',(p.tenant,))]

    def _issue(self, db, row, kind='session'):
        token = secrets.token_urlsafe(36)
        expiry = db.now()+self.session_seconds
        db.execute('INSERT INTO desk_sessions VALUES(?,?,?,?,?,0,?,?)',
                   (hashlib.sha256(token.encode()).hexdigest(),row['tenant'],row['user_id'],row['epoch'],expiry,kind,db.now()))
        return {'token':token,'expires_at':expiry,'principal':{'tenant':row['tenant'],'user':row['user_id'],'role':row['role']},'display_name':row['display_name']}

    def login(self, tenant, user, password, peer='local'):
        if not NAME.fullmatch(tenant) or not NAME.fullmatch(user): return None
        bucket = hashlib.sha256(encode([tenant,user,peer]).encode()).hexdigest()
        result = None
        with self.store.db(True) as db:
            now = db.now()
            db.execute('INSERT INTO login_attempts VALUES(?,0,?,0) ON CONFLICT(id) DO NOTHING',(bucket,now))
            attempt = db.execute(db.locked('SELECT * FROM login_attempts WHERE id=?'),(bucket,)).fetchone()
            if attempt['blocked_until'] > now: return None
            row = db.execute(db.locked('SELECT * FROM desk_accounts WHERE tenant=? AND user_id=?'),(tenant,user)).fetchone()
            stored = row['password_hash'] if row else 'pbkdf2_sha256$600000$'+'0'*32+'$'+'0'*64
            valid = password_matches(password,stored)
            if row and row['enabled'] and valid:
                db.execute('UPDATE login_attempts SET failures=0,window_start=?,blocked_until=0 WHERE id=?',(now,bucket))
                result = self._issue(db,row)
                self.audit(db,Principal(tenant,user,row['role']),'session.login',user)
            else:
                failures = attempt['failures']+1 if now-attempt['window_start']<300 else 1
                db.execute('UPDATE login_attempts SET failures=?,window_start=?,blocked_until=? WHERE id=?',
                           (failures,attempt['window_start'] if failures>1 else now,now+300 if failures>=5 else 0,bucket))
        return result

    def demo_login(self, role):
        if not demo_enabled(): raise PermissionError('测试身份切换未启用')
        if role not in {'user','support'}: raise ValueError('无效角色')
        user = 'learner' if role == 'user' else 'support'
        with self.store.db(True) as db:
            row = db.execute(db.locked('SELECT * FROM desk_accounts WHERE tenant=? AND user_id=?'),('demo',user)).fetchone()
            if not row or not row['enabled'] or row['role'] != role: raise PermissionError('测试账号不可用')
            result = self._issue(db,row,'demo')
            self.audit(db,Principal('demo',user,role),'session.demo_login',user)
            return result

    def authenticate(self, token):
        if not token or len(token)>512: return None
        with self.store.db() as db:
            row = db.execute('''SELECT a.tenant,a.user_id,a.role FROM desk_sessions s JOIN desk_accounts a
                ON s.tenant=a.tenant AND s.user_id=a.user_id WHERE s.token_hash=? AND s.revoked=0
                AND s.expires>? AND a.enabled=1 AND s.epoch=a.epoch''',(hashlib.sha256(token.encode()).hexdigest(),db.now())).fetchone()
            return Principal(row['tenant'],row['user_id'],row['role']) if row else None

    def logout(self, token):
        with self.store.db(True) as db:
            db.execute('UPDATE desk_sessions SET revoked=1 WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))

    def set_enabled(self, p, user, enabled):
        if p.role != 'support': raise PermissionError()
        if p.user == user: raise ValueError('不能禁用当前管理账号')
        with self.store.db(True) as db:
            row = db.execute(db.locked('SELECT * FROM desk_accounts WHERE tenant=? AND user_id=?'),(p.tenant,user)).fetchone()
            if row is None: raise KeyError('Account unavailable')
            db.execute('UPDATE desk_accounts SET enabled=?,epoch=epoch+1,updated=? WHERE tenant=? AND user_id=?',(int(enabled),db.now(),p.tenant,user))
            if not enabled:
                from resolvedesk.business import offboard_access
                offboard_access(db,p,user)
            self.audit(db,p,'account.enable' if enabled else 'account.disable',user)

    def revoke_sessions(self, p, user):
        if p.role != 'support' and p.user != user: raise PermissionError()
        with self.store.db(True) as db:
            count=db.execute('UPDATE desk_accounts SET epoch=epoch+1,updated=? WHERE tenant=? AND user_id=?',(db.now(),p.tenant,user)).rowcount
            if not count: raise KeyError('Account unavailable')
            self.audit(db,p,'session.revoke_all',user)

    def audit_list(self,p):
        if p.role != 'support': raise PermissionError()
        with self.store.db() as db:
            return [dict(row,detail=json.loads(row['detail'])) for row in db.execute('SELECT * FROM security_audit WHERE tenant=? ORDER BY created DESC LIMIT 100',(p.tenant,))]


def resolve_identity(state, token):
    identity = getattr(state,'identity',None)
    if identity is not None:
        return identity.authenticate(token)
    # Explicit injected/offline API fixtures retain their existing token contract.
    account = state.accounts.get(hashlib.sha256(token.encode()).hexdigest())
    return Principal(account['tenant'],account['user'],account['role']) if account and account.get('role') in {'user','support'} else None
