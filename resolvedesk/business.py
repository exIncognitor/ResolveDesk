"""Synthetic IT service domain. All writes are local, transactional and audited."""
from __future__ import annotations

import json
import time

from resolvedesk.store import Conflict, digest, encode


RESOURCES = {
    "repo-resolvedesk": {"name": "ResolveDesk 开发仓库", "levels": ["read", "write"], "max_days": 30},
    "docs-internal": {"name": "内部技术文档", "levels": ["read"], "max_days": 30},
}
LICENSES = [
    {"product": "IntelliJ IDEA Ultimate", "available_seats": 2, "approval": "主管审批后由 IT 分配", "personal_purchase_reimbursement": False},
    {"product": "VS Code", "available_seats": None, "approval": "按软件白名单安装；商业插件另行申请"},
]


def initialize(db):
    from resolvedesk.licenses import initialize as initialize_licenses
    initialize_licenses(db)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS desk_assets(tenant TEXT,id TEXT,owner TEXT,model TEXT,warranty TEXT,status TEXT,
            PRIMARY KEY(tenant,id));
        CREATE TABLE IF NOT EXISTS service_cases(ticket_id TEXT PRIMARY KEY,tenant TEXT,owner TEXT,kind TEXT,
            status TEXT,version INTEGER,fields TEXT,assignee TEXT,updated REAL);
        CREATE TABLE IF NOT EXISTS service_audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,ticket_id TEXT,tenant TEXT,
            actor TEXT,action TEXT,from_status TEXT,to_status TEXT,note TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS service_actions(tenant TEXT,actor TEXT,ticket_id TEXT,request_key TEXT,
            fingerprint TEXT,response TEXT,PRIMARY KEY(tenant,actor,ticket_id,request_key));
        CREATE TABLE IF NOT EXISTS desk_grants(tenant TEXT,owner TEXT,resource TEXT,level TEXT,expires REAL,
            source_ticket TEXT,PRIMARY KEY(tenant,owner,resource));
        CREATE TABLE IF NOT EXISTS grant_sources(ticket_id TEXT PRIMARY KEY,tenant TEXT,owner TEXT,resource TEXT,
            level TEXT,expires REAL,state TEXT,updated REAL);
        CREATE INDEX IF NOT EXISTS grant_owner ON grant_sources(tenant,owner,resource,state,expires);
        CREATE TABLE IF NOT EXISTS grant_locks(tenant TEXT,owner TEXT,resource TEXT,PRIMARY KEY(tenant,owner,resource));
    """)
    db.execute("""INSERT INTO grant_sources SELECT source_ticket,tenant,owner,resource,level,expires,'active',?
        FROM desk_grants WHERE source_ticket IS NOT NULL AND 1=1 ON CONFLICT(ticket_id) DO NOTHING""",(db.now(),))


def effective_grants(db,tenant,owner):
    rows=[dict(r) for r in db.execute("SELECT * FROM grant_sources WHERE tenant=? AND owner=? AND state='active' AND expires>?",(tenant,owner,db.now()))]
    result=[]
    for resource in sorted({r['resource'] for r in rows}):
        candidates=[r for r in rows if r['resource']==resource]
        level='write' if any(r['level']=='write' for r in candidates) else 'read'
        chosen=max((r for r in candidates if r['level']==level),key=lambda r:r['expires'])
        result.append({'resource':resource,'level':level,'expires':chosen['expires'],'source_ticket':chosen['ticket_id']})
    return result


def refresh_grants(db,tenant,owner):
    db.execute('UPDATE desk_grants SET expires=? WHERE tenant=? AND owner=?',(db.now(),tenant,owner))
    for grant in effective_grants(db,tenant,owner):
        db.execute('''INSERT INTO desk_grants VALUES(?,?,?,?,?,?) ON CONFLICT(tenant,owner,resource)
            DO UPDATE SET level=excluded.level,expires=excluded.expires,source_ticket=excluded.source_ticket''',
            (tenant,owner,grant['resource'],grant['level'],grant['expires'],grant['source_ticket']))


def lock_grant(db,tenant,owner,resource):
    db.execute('INSERT INTO grant_locks VALUES(?,?,?) ON CONFLICT(tenant,owner,resource) DO NOTHING',(tenant,owner,resource))
    db.execute(db.locked('SELECT * FROM grant_locks WHERE tenant=? AND owner=? AND resource=?'),(tenant,owner,resource)).fetchone()


def offboard_access(db,p,user):
    """Disable access and release software seats atomically with account disablement."""
    rows=db.execute("SELECT * FROM service_cases WHERE tenant=? AND owner=? AND ((kind='access_request' AND status IN ('pending_approval','approved','fulfilled')) OR (kind='license_request' AND status IN ('pending_approval','approved','reserved','allocated')))",(p.tenant,user)).fetchall()
    db.execute("UPDATE grant_sources SET state='revoked',updated=? WHERE tenant=? AND owner=? AND state='active'",(db.now(),p.tenant,user))
    refresh_grants(db,p.tenant,user)
    db.execute("UPDATE license_allocations SET state='revoked',expires=?,updated=? WHERE tenant=? AND owner=? AND state IN ('reserved','allocated')",(db.now(),db.now(),p.tenant,user))
    for row in rows:
        target='revoked' if row['status'] in {'fulfilled','reserved','allocated'} else 'cancelled'
        db.execute("UPDATE service_cases SET status=?,version=version+1,updated=? WHERE ticket_id=?",(target,db.now(),row['ticket_id']))
        db.execute("UPDATE tickets SET status=? WHERE id=? AND tenant=?",(target,row['ticket_id'],p.tenant))
        BusinessService._audit(db,p,row['ticket_id'],'revoke' if target=='revoked' else 'cancel',row['status'],target,'账号禁用：回收有效资源并取消未完成资源申请')
    active=db.execute("SELECT * FROM runs WHERE tenant=? AND owner=? AND status IN ('queued','running')",(p.tenant,user)).fetchall()
    for run in active:
        db.execute("UPDATE runs SET status='paused',error='account_disabled',execution_epoch=execution_epoch+1,execution_owner=NULL,lease_until=0,updated=? WHERE id=?",(db.now(),run['id']))
        current=db.execute('SELECT * FROM runs WHERE id=?',(run['id'],)).fetchone()
        from resolvedesk.store import Store
        Store._event(db,run['id'],'account_disabled',{})
        Store._receipt(db,current)


def classify(message):
    """Auditable domain routing; multi-domain input retains every matching domain."""
    text = message.lower()
    patterns = {
        "account_access": ("权限", "账号", "密码", "账户", "仓库", "mfa", "解锁"),
        "network_endpoint": ("vpn", "网络", "dns", "断网", "蓝屏", "终端", "连接超时", "401", "691"),
        "software_license": ("授权", "许可证", "license", "席位", "订阅", "扣款", "退款", "发票", "pip", "ide", "软件"),
        "device_service": ("设备", "维修", "笔记本", "电池", "资产", "lt-", "外设"),
    }
    domains = [name for name, terms in patterns.items() if any(t in text for t in terms)]
    if "人工" in text or "泄露" in text or "大面积" in text:
        domains.insert(0, "escalation")
    return {"intent": domains[0] if domains else "general", "domains": domains or ["general"],
            "method": "versioned_it_rules_v1"}


class BusinessService:
    def __init__(self, store, directory=None):
        self.store,self.directory = store,directory

    def seed_demo(self):
        # Explicit seed only, never creates employee assets in arbitrary tenants.
        with self.store.db(True) as db:
            for asset_id, owner, model, warranty in (
                ("LT-1001", "learner", "演示开发笔记本", "in_warranty"),
                ("LT-1002", "support", "演示运维笔记本", "expired"),
            ):
                db.execute("INSERT INTO desk_assets VALUES('demo',?,?,?,?, 'available') ON CONFLICT(tenant,id) DO NOTHING",
                           (asset_id, owner, model, warranty))

    def context(self, p):
        from resolvedesk.licenses import catalog
        with self.store.db() as db:
            license_catalog=catalog(db,p)
            assets = [dict(r) for r in db.execute("SELECT * FROM desk_assets WHERE tenant=? AND owner=?", (p.tenant, p.user))]
            grants = effective_grants(db,p.tenant,p.user)
        return {"simulation": True, "notice": "模拟企业制度和本地业务系统，不会更改真实账号或设备。",
                "resources": RESOURCES, "licenses": [license_catalog]+[entry for entry in LICENSES if entry['product']!=license_catalog['product']], "my_assets": assets, "my_grants": grants}

    @staticmethod
    def validate(db, p, fields):
        kind = fields.get("workflow", "generic")
        if kind == "generic":
            return []
        missing = []
        if kind == "access_request":
            if fields.get("category") != "access":
                raise ValueError("权限申请必须选择 access 类别")
            resource = fields.get("resource_id", "")
            level = fields.get("access_level", "")
            days = fields.get("duration_days", 0)
            if not resource: missing.append("资源 ID")
            elif resource not in RESOURCES: raise ValueError("资源不在可申请目录")
            if not level: missing.append("权限级别")
            elif resource in RESOURCES and level not in RESOURCES[resource]["levels"]:
                raise ValueError("该资源不允许申请此权限级别")
            if not days: missing.append("权限有效天数")
            elif type(days) is not int or not 1 <= days <= RESOURCES.get(resource, {"max_days": 30})["max_days"]:
                raise ValueError("权限有效期为 1 至 30 天")
        elif kind == 'license_request':
            from resolvedesk.licenses import validate
            missing.extend(validate(fields))
        elif kind == "device_repair":
            if fields.get("category") != "hardware":
                raise ValueError("设备维修必须选择 hardware 类别")
            asset_id = fields.get("asset_id", "")
            if not asset_id: missing.append("设备资产 ID")
            else:
                asset = db.execute(db.locked("SELECT * FROM desk_assets WHERE tenant=? AND id=? AND owner=?"), (p.tenant, asset_id, p.user)).fetchone()
                if asset is None: raise ValueError("设备不属于申请人或资产不存在")
                if asset["status"] != "available": raise Conflict("设备已有未完成的维修流程")
        else:
            raise ValueError("未知业务流程")
        return missing

    def validate_draft(self, p, fields):
        with self.store.db() as db:
            return self.validate(db, p, fields)

    @staticmethod
    def materialize(db, p, ticket_id, fields):
        kind = fields.get("workflow", "generic")
        if kind == "generic": return
        missing = BusinessService.validate(db, p, fields)
        if missing: raise ValueError("业务字段不完整：" + "、".join(missing))
        state = "pending_approval" if kind in {"access_request","license_request"} else "submitted"
        if kind == "device_repair":
            db.execute("UPDATE desk_assets SET status='in_repair' WHERE tenant=? AND id=?", (p.tenant, fields["asset_id"]))
        db.execute("INSERT INTO service_cases VALUES(?,?,?,?,?,1,?,NULL,?)", (ticket_id, p.tenant, p.user, kind, state, encode(fields), time.time()))
        db.execute("UPDATE tickets SET status=? WHERE id=? AND tenant=?", (state, ticket_id, p.tenant))
        BusinessService._audit(db, p, ticket_id, "submit", None, state, "申请人已核对草稿并确认；模拟业务")

    @staticmethod
    def _audit(db, p, ticket_id, action, old, new, note):
        db.execute("INSERT INTO service_audit(ticket_id,tenant,actor,action,from_status,to_status,note,created) VALUES(?,?,?,?,?,?,?,?)",
                   (ticket_id, p.tenant, p.user, action, old, new, note, time.time()))

    @staticmethod
    def _get(db, p, ticket_id):
        scope = "" if p.role == "support" else " AND owner=?"
        params = [ticket_id, p.tenant] + ([] if p.role == "support" else [p.user])
        row = db.execute(db.locked("SELECT * FROM service_cases WHERE ticket_id=? AND tenant=?" + scope), params).fetchone()
        if row is None: raise KeyError("Service case not found")
        return row

    def _public(self, db, p, row):
        result = dict(row, fields=json.loads(row["fields"]), simulation=True)
        if row['kind']=='license_request':
            slot=db.execute('SELECT state,expires,product FROM license_allocations WHERE ticket_id=? AND tenant=?',(row['ticket_id'],p.tenant)).fetchone()
            result['license']=dict(slot) if slot else None
        result["audit"] = [dict(r) for r in db.execute("SELECT actor,action,from_status,to_status,note,created FROM service_audit WHERE ticket_id=? AND tenant=? ORDER BY seq", (row["ticket_id"], p.tenant))]
        result["allowed_actions"] = self.actions(p, row)
        return result

    def actions(self,p,row):
        result=self.allowed(p,row)
        if self.directory and p.role=='support' and row['kind']=='device_repair' and row['status']=='resolved' and row['assignee']==p.user:
            member=self.directory.member(p.tenant,row['owner'])
            if member and not member['enabled'] and 'accept' not in result:result.append('accept')
        return result

    @staticmethod
    def allowed(p, row):
        if row['kind']=='license_request':
            from resolvedesk.licenses import allowed
            return allowed(p,row)
        state, kind = row["status"], row["kind"]
        actions = []
        if row["owner"] == p.user:
            if state in {"submitted", "pending_approval"}: actions.append("cancel")
            if state == "resolved": actions += ["accept", "reopen"]
            if kind == 'access_request' and state == 'approved': actions.append('cancel')
            if kind == 'access_request' and state == 'fulfilled': actions.append('revoke')
        if p.role == "support":
            if kind == "access_request":
                if state == "pending_approval" and row["owner"] != p.user: actions += ["approve", "reject"]
                if state == "approved" and row["owner"] != p.user: actions.append("provision")
                if state == 'fulfilled' and 'revoke' not in actions: actions.append('revoke')
            else:
                actions += {"submitted": ["triage"], "triaged": ["assign"], "assigned": ["start"], "in_progress": ["resolve"]}.get(state, [])
                if state in {'assigned','in_progress'}: actions.append('assign')
                if row["assignee"] != p.user:
                    actions = [action for action in actions if action not in {"start", "resolve"}]
        return actions

    def get(self, p, ticket_id):
        from resolvedesk.licenses import expire_case
        with self.store.db(True) as db: return self._public(db, p, expire_case(db,self._get(db, p, ticket_id)))

    def list(self, p):
        from resolvedesk.licenses import expire_due
        expire_due(self.store)
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM service_cases WHERE tenant=?" + ("" if p.role == "support" else " AND owner=?") + " ORDER BY updated DESC LIMIT 100",
                              [p.tenant] + ([] if p.role == "support" else [p.user])).fetchall()
            return [self._public(db, p, r) for r in rows]

    def transition(self, p, ticket_id, action, version, request_key, note="", assignee=""):
        if not note.strip(): raise ValueError("请填写处理或验收说明")
        fingerprint = digest([action, version, note, assignee])
        with self.store.db(True) as db:
            if self.directory:
                # Account first, then case/resource: the same order as disablement.
                owner=db.execute('SELECT owner FROM service_cases WHERE ticket_id=? AND tenant=?',(ticket_id,p.tenant)).fetchone()
                if owner is not None:
                    if p.role!='support' and owner['owner']!=p.user: raise KeyError('Service case not found')
                    db.execute(db.locked('SELECT user_id FROM desk_accounts WHERE tenant=? AND user_id=?'),(p.tenant,owner['owner'])).fetchone()
            row = self._get(db, p, ticket_id)
            previous = db.execute("SELECT fingerprint,response FROM service_actions WHERE tenant=? AND actor=? AND ticket_id=? AND request_key=?",
                                  (p.tenant, p.user, ticket_id, request_key)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint: raise Conflict("幂等键已经用于其他操作")
                return json.loads(previous["response"])
            from resolvedesk.licenses import expire_case
            row=expire_case(db,row)
            if row["version"] != version: raise Conflict("流程已更新，请刷新后操作")
            if action not in self.actions(p, row): raise Conflict("当前状态或角色不允许此操作")
            fields = json.loads(row["fields"])
            if self.directory and action in {'approve','provision','reserve','allocate'}:
                owner=self.directory.member(p.tenant,row['owner'])
                if not owner or not owner['enabled']: raise ValueError('申请人账号已停用，不能授予资源')
            targets = {"approve": "approved", "reject": "rejected", "provision": "fulfilled", "cancel": "cancelled",
                       "triage": "triaged", "assign": "assigned", "start": "in_progress", "resolve": "resolved", "accept": "closed", "reopen": "in_progress",
                       "reserve":"reserved","allocate":"allocated","revoke":"revoked"}
            target = targets[action]
            if row['kind']=='license_request':
                from resolvedesk.licenses import apply
                apply(db,p,row,fields,action)
            if action == "assign":
                member=self.directory.member(p.tenant,assignee) if self.directory else None
                if self.directory:
                    if not member or not member['enabled'] or member['role']!='support': raise ValueError('处理人必须是本租户启用的支持人员')
                elif assignee != p.user: raise ValueError("离线模式仅支持分派给当前操作员")
            if action in {"start", "resolve"} and row["assignee"] != p.user:
                raise PermissionError("只有被分派的 IT 操作员可以处理")
            if action == "provision":
                if self.directory:
                    member=self.directory.member(p.tenant,row['owner'])
                    if not member or not member['enabled']: raise ValueError('申请人账号已停用，不能开通授权')
                missing = self.validate(db, p, fields)
                if missing: raise ValueError("申请不完整")
                lock_grant(db,p.tenant,row['owner'],fields['resource_id'])
                db.execute("INSERT INTO grant_sources VALUES(?,?,?,?,?,?,'active',?)",
                           (ticket_id,p.tenant,row['owner'],fields['resource_id'],fields['access_level'],db.now()+fields['duration_days']*86400,db.now()))
                refresh_grants(db,p.tenant,row['owner'])
            if action == 'revoke' and row['kind']=='access_request':
                lock_grant(db,p.tenant,row['owner'],fields['resource_id'])
                db.execute("UPDATE grant_sources SET state='revoked',updated=? WHERE ticket_id=? AND tenant=?",(db.now(),ticket_id,p.tenant))
                refresh_grants(db,p.tenant,row['owner'])
            if row["kind"] == "device_repair" and action in {"accept", "cancel"}:
                db.execute("UPDATE desk_assets SET status='available' WHERE tenant=? AND id=?", (p.tenant, fields["asset_id"]))
                if action=='accept' and row['owner']!=p.user:note='代验收（申请人账号已停用）：'+note
            db.execute("UPDATE service_cases SET status=?,version=version+1,assignee=?,updated=? WHERE ticket_id=?",
                       (target, assignee if action == "assign" else row["assignee"], time.time(), ticket_id))
            db.execute("UPDATE tickets SET status=? WHERE id=? AND tenant=?", (target, ticket_id, p.tenant))
            self._audit(db, p, ticket_id, action, row["status"], target, note)
            result = self._public(db, p, self._get(db, p, ticket_id))
            db.execute("INSERT INTO service_actions VALUES(?,?,?,?,?,?)", (p.tenant, p.user, ticket_id, request_key, fingerprint, encode(result)))
            return result
