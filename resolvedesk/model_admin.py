"""Tenant-scoped model profiles and durable token-based cost estimates, without caps."""
from contextlib import asynccontextmanager
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

from core.model_client import ModelClient, model_identity
from resolvedesk.identity import IdentityService
from resolvedesk.store import Conflict, encode

MODEL_TABLES = {'model_profiles', 'model_tenants', 'model_usage'}


def validated_profile(value):
    if value['protocol'] not in {'anthropic','openai'} or not value['model'].strip() or len(value['model'])>100:
        raise ValueError('请填写有效协议与模型名称')
    url = urlsplit(value['base_url'])
    if url.scheme not in {'http','https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('模型地址必须是不含凭据、查询参数或片段的HTTP地址')
    if url.scheme == 'http' and url.hostname not in {'localhost','127.0.0.1','::1'}:
        raise ValueError('远程模型服务必须使用HTTPS')
    allowed = {host.strip().lower() for host in os.getenv('RESOLVEDESK_MODEL_ALLOWED_HOSTS','').split(',') if host.strip()}
    if allowed and url.hostname.lower() not in allowed:
        raise ValueError('模型服务域名未加入服务端允许清单')
    if value['currency'] not in {'CNY','USD'}: raise ValueError('费用币种仅支持CNY或USD')
    prices = {}
    for name in ('input','output','cached_input'):
        raw = value.get('prices',{}).get(name)
        try:
            number = Decimal(str(raw)) if raw not in {None,''} else None
            if number is not None and (not number.is_finite() or not 0 <= number <= 100000): raise ValueError()
        except (InvalidOperation, ValueError): raise ValueError('单价需为有效非负数，单位为每百万Token') from None
        prices[name] = str(number) if number is not None else None
    return dict(value, model=value['model'].strip(),base_url=value['base_url'].rstrip('/'),prices=prices)


class ModelRegistry:
    def __init__(self, store, key=None):
        self.store = store
        if key is None:
            value = os.getenv('RESOLVEDESK_CONFIG_ENCRYPTION_KEY')
            key = value.encode() if value else (Path(__file__).resolve().parents[1]/'data/config-encryption.key').read_bytes().strip()
        self.cipher = Fernet(key)
        with store.database.schema_transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS model_profiles(id TEXT PRIMARY KEY,tenant TEXT,name TEXT,protocol TEXT,
                    model TEXT,base_url TEXT,key_cipher TEXT,currency TEXT,prices TEXT,revision INTEGER,created REAL,updated REAL);
                CREATE INDEX IF NOT EXISTS model_profile_tenant ON model_profiles(tenant,created);
                CREATE TABLE IF NOT EXISTS model_tenants(tenant TEXT PRIMARY KEY,active_profile TEXT,revision INTEGER);
                CREATE TABLE IF NOT EXISTS model_usage(id TEXT PRIMARY KEY,tenant TEXT,owner TEXT,context TEXT,profile_id TEXT,
                    profile_revision INTEGER,model TEXT,currency TEXT,status TEXT,input_tokens INTEGER,output_tokens INTEGER,
                    cached_tokens INTEGER,estimated_cost TEXT,prices TEXT,created REAL,finished REAL);
                CREATE INDEX IF NOT EXISTS model_usage_tenant ON model_usage(tenant,created);
            ''')

    def _default(self):
        client = ModelClient()
        return {'id':'environment','revision':0,'name':'环境默认模型',**client.describe(),'key':client._key,
                'currency':'CNY','prices':{'input':None,'output':None,'cached_input':None}}

    def active(self, p):
        with self.store.db() as db:
            row = db.execute('''SELECT m.* FROM model_tenants t JOIN model_profiles m ON t.active_profile=m.id
                AND t.tenant=m.tenant WHERE t.tenant=?''',(p.tenant,)).fetchone()
        if row is None: return self._default()
        return dict(row,key=self.cipher.decrypt(row['key_cipher'].encode()).decode() if row['key_cipher'] else '',prices=json.loads(row['prices']))

    @staticmethod
    def public(profile):
        return {key:profile[key] for key in ('id','name','protocol','model','base_url','currency','prices','revision')} | {'configured':bool(profile.get('key') or profile.get('key_cipher'))}

    def describe(self,p):
        return self.public(self.active(p))

    def list(self,p):
        if p.role != 'support': raise PermissionError()
        with self.store.db() as db:
            rows = [dict(row,prices=json.loads(row['prices'])) for row in db.execute('SELECT * FROM model_profiles WHERE tenant=? ORDER BY created',(p.tenant,))]
        active = self.active(p)
        return {'active':self.public(active),'profiles':[self.public(self._default())]+[self.public(row) for row in rows],
                'pricing_note':'按每次调用时保存的单价和供应商返回的Token估算，不代表供应商账单或余额；未配置单价或缺少usage时不计为零。'}

    def save(self,p,value,profile_id=None,revision=None):
        if p.role != 'support': raise PermissionError()
        value = validated_profile(value)
        key = value.get('api_key') or ''
        if len(key)>8192 or not value.get('name','').strip() or len(value['name'])>100: raise ValueError('配置名称或密钥无效')
        with self.store.db(True) as db:
            row = db.execute(db.locked('SELECT * FROM model_profiles WHERE id=? AND tenant=?'),(profile_id,p.tenant)).fetchone() if profile_id else None
            if profile_id and row is None: raise KeyError('Profile unavailable')
            if row and row['revision'] != revision: raise Conflict('模型配置已变更，请刷新')
            if not row and db.execute('SELECT COUNT(*) FROM model_profiles WHERE tenant=?',(p.tenant,)).fetchone()[0]>=20:
                raise ValueError('最多保存20个模型配置')
            cipher = self.cipher.encrypt(key.encode()).decode() if key else row['key_cipher'] if row else ''
            if not cipher: raise ValueError('首次保存请填写模型API Key')
            identity = profile_id or secrets.token_hex(16)
            rev = row['revision']+1 if row else 1
            db.execute('''INSERT INTO model_profiles VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,protocol=excluded.protocol,model=excluded.model,base_url=excluded.base_url,key_cipher=excluded.key_cipher,
                currency=excluded.currency,prices=excluded.prices,revision=excluded.revision,updated=excluded.updated''',
                (identity,p.tenant,value['name'],value['protocol'],value['model'],value['base_url'],cipher,value['currency'],encode(value['prices']),rev,db.now(),db.now()))
            IdentityService.audit(db,p,'model.update' if row else 'model.create',identity,{'model':value['model'],'revision':rev})
        return {'id':identity,'revision':rev}

    def activate(self,p,profile_id,revision):
        if p.role != 'support': raise PermissionError()
        with self.store.db(True) as db:
            db.execute('INSERT INTO model_tenants VALUES(?,NULL,0) ON CONFLICT(tenant) DO NOTHING',(p.tenant,))
            db.execute(db.locked('SELECT * FROM model_tenants WHERE tenant=?'),(p.tenant,)).fetchone()
            if profile_id == 'environment':
                if revision != 0: raise Conflict('默认配置版本不正确')
                db.execute('UPDATE model_tenants SET active_profile=NULL,revision=revision+1 WHERE tenant=?',(p.tenant,))
                IdentityService.audit(db,p,'model.activate','environment')
                return self.public(self._default())
            row = db.execute(db.locked('SELECT * FROM model_profiles WHERE id=? AND tenant=?'),(profile_id,p.tenant)).fetchone()
            if not row: raise KeyError('Profile unavailable')
            if row['revision'] != revision: raise Conflict('模型配置已变更，请刷新')
            db.execute('UPDATE model_tenants SET active_profile=?,revision=revision+1 WHERE tenant=?',(profile_id,p.tenant))
            IdentityService.audit(db,p,'model.activate',profile_id,{'model':row['model'],'revision':revision})
        return self.describe(p)

    def for_actor(self,p,context):
        profile = self.active(p)
        observer = UsageRecorder(self.store,p,context,profile,getattr(self,'identity',None))
        return ModelClient(protocol=profile['protocol'],model=profile['model'],base_url=profile['base_url'],api_key=profile['key'],usage_observer=observer,timeout=25)

    def usage(self,p,days=30):
        if p.role != 'support': raise PermissionError()
        if type(days) is not int or not 1<=days<=366: raise ValueError('查询天数为1至366')
        with self.store.db() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM model_usage WHERE tenant=? AND created>=? ORDER BY created DESC LIMIT 10000',(p.tenant,db.now()-days*86400))]
            total = db.execute('SELECT COUNT(*) FROM model_usage WHERE tenant=? AND created>=?',(p.tenant,db.now()-days*86400)).fetchone()[0]
        currencies = {}
        for row in rows:
            if row['estimated_cost'] is not None:
                currencies[row['currency']] = str(Decimal(currencies.get(row['currency'],'0'))+Decimal(row['estimated_cost']))
        return {'days':days,'calls':total,'observed_calls':len(rows),'truncated':total>len(rows),
                'estimated_costs':currencies,'unknown_cost_calls':sum(row['estimated_cost'] is None for row in rows),
                'input_tokens':sum(row['input_tokens'] or 0 for row in rows),'output_tokens':sum(row['output_tokens'] or 0 for row in rows),
                'cached_tokens':sum(row['cached_tokens'] or 0 for row in rows),'recent':[{k:v for k,v in row.items() if k!='prices'} for row in rows[:100]],
                'note':'持久化调用记录；费用为估算，不是供应商账单。缺失usage、失败或未配置单价的调用费用未知。最多汇总最近10000条，超出时明确标记截断。'}


class UsageRecorder:
    def __init__(self,store,p,context,profile,directory=None):
        self.store,self.p,self.context,self.profile = store,p,context,profile
        self.directory=directory

    def begin(self):
        if self.directory:
            account=self.directory.member(self.p.tenant,self.p.user)
            if not account or not account['enabled']: raise PermissionError('账号已停用，不能继续调用模型')
        identity = secrets.token_hex(16)
        with self.store.db(True) as db:
            db.execute('INSERT INTO model_usage VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?,?,NULL)',
                       (identity,self.p.tenant,self.p.user,self.context,self.profile['id'],self.profile['revision'],self.profile['model'],self.profile['currency'],'started',encode(self.profile['prices']),db.now()))
        return identity

    def finish(self,identity,response=None):
        usage = getattr(response,'usage',None)
        inputs,outputs = getattr(usage,'input_tokens',None),getattr(usage,'output_tokens',None)
        cached = getattr(usage,'cache_read_input_tokens',0) or 0
        valid = all(type(n) is int and n>=0 for n in (inputs,outputs,cached))
        cost = None
        prices = self.profile['prices']
        if valid and not getattr(usage,'cache_creation_input_tokens',0) and all(prices.get(k) is not None for k in ('input','output','cached_input')):
            # Anthropic input excludes cache hits; OpenAI prompt_tokens includes them.
            uncached = max(0,inputs-cached) if self.profile['protocol']=='openai' else inputs
            cost = str((Decimal(uncached)*Decimal(prices['input'])+Decimal(outputs)*Decimal(prices['output'])+Decimal(cached)*Decimal(prices['cached_input']))/Decimal(1000000))
        with self.store.db(True) as db:
            db.execute('UPDATE model_usage SET status=?,input_tokens=?,output_tokens=?,cached_tokens=?,estimated_cost=?,finished=? WHERE id=?',
                       ('completed' if response is not None else 'failed',inputs if valid else None,outputs if valid else None,cached if valid else None,cost,db.now(),identity))


class ManagedModelClient:
    """Each run pins a profile; activation affects subsequent runs on every worker."""
    def __init__(self,registry):
        self.registry = registry
        self._bound = ContextVar('resolvedesk_model_binding_'+secrets.token_hex(8),default=None)
        self.messages = self

    @property
    def model(self): return self._current().model

    def _current(self): return self._bound.get() or ModelClient()

    def model_identity(self): return model_identity(self._current())

    def describe(self): return self._current().describe()

    def usage_summary(self): return {'scope':'tenant_persistent_usage_endpoint'}

    @asynccontextmanager
    async def scope(self,p,context):
        existing = self._bound.get()
        if existing is not None:
            yield existing
            return
        client = self.registry.for_actor(p,context)
        marker = self._bound.set(client)
        try: yield client
        finally:
            self._bound.reset(marker)
            await client.close()

    async def create(self,**kwargs):
        client = self._bound.get()
        if client is None: raise RuntimeError('Authenticated model scope required')
        return await client.messages.create(**kwargs)

    @asynccontextmanager
    async def stream(self,**kwargs):
        client = self._bound.get()
        if client is None: raise RuntimeError('Authenticated model scope required')
        async with client.messages.stream(**kwargs) as stream: yield stream

    async def close(self): pass
