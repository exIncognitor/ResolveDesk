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
    """)


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
    def __init__(self, store):
        self.store = store

    def seed_demo(self):
        # Explicit seed only, never creates employee assets in arbitrary tenants.
        with self.store.db(True) as db:
            for asset_id, owner, model, warranty in (
                ("LT-1001", "learner", "演示开发笔记本", "in_warranty"),
                ("LT-1002", "support", "演示运维笔记本", "expired"),
            ):
                db.execute("INSERT OR IGNORE INTO desk_assets VALUES('demo',?,?,?,?, 'available')",
                           (asset_id, owner, model, warranty))

    def context(self, p):
        with self.store.db() as db:
            assets = [dict(r) for r in db.execute("SELECT * FROM desk_assets WHERE tenant=? AND owner=?", (p.tenant, p.user))]
            grants = [dict(r) for r in db.execute("SELECT resource,level,expires,source_ticket FROM desk_grants WHERE tenant=? AND owner=? AND expires>?",
                                                (p.tenant, p.user, time.time()))]
        return {"simulation": True, "notice": "模拟企业制度和本地业务系统，不会更改真实账号或设备。",
                "resources": RESOURCES, "licenses": LICENSES, "my_assets": assets, "my_grants": grants}

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
        elif kind == "device_repair":
            if fields.get("category") != "hardware":
                raise ValueError("设备维修必须选择 hardware 类别")
            asset_id = fields.get("asset_id", "")
            if not asset_id: missing.append("设备资产 ID")
            else:
                asset = db.execute("SELECT * FROM desk_assets WHERE tenant=? AND id=? AND owner=?", (p.tenant, asset_id, p.user)).fetchone()
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
        state = "pending_approval" if kind == "access_request" else "submitted"
        if kind == "device_repair":
            db.execute("UPDATE desk_assets SET status='in_repair' WHERE tenant=? AND id=?", (p.tenant, fields["asset_id"]))
        db.execute("INSERT INTO service_cases VALUES(?,?,?,?,?,1,?,NULL,?)", (ticket_id, p.tenant, p.user, kind, state, encode(fields), time.time()))
        BusinessService._audit(db, p, ticket_id, "submit", None, state, "申请人已核对草稿并确认；模拟业务")

    @staticmethod
    def _audit(db, p, ticket_id, action, old, new, note):
        db.execute("INSERT INTO service_audit(ticket_id,tenant,actor,action,from_status,to_status,note,created) VALUES(?,?,?,?,?,?,?,?)",
                   (ticket_id, p.tenant, p.user, action, old, new, note, time.time()))

    @staticmethod
    def _get(db, p, ticket_id):
        scope = "" if p.role == "support" else " AND owner=?"
        params = [ticket_id, p.tenant] + ([] if p.role == "support" else [p.user])
        row = db.execute("SELECT * FROM service_cases WHERE ticket_id=? AND tenant=?" + scope, params).fetchone()
        if row is None: raise KeyError("Service case not found")
        return row

    @staticmethod
    def _public(db, p, row):
        result = dict(row, fields=json.loads(row["fields"]), simulation=True)
        result["audit"] = [dict(r) for r in db.execute("SELECT actor,action,from_status,to_status,note,created FROM service_audit WHERE ticket_id=? AND tenant=? ORDER BY seq", (row["ticket_id"], p.tenant))]
        result["allowed_actions"] = BusinessService.allowed(p, row)
        return result

    @staticmethod
    def allowed(p, row):
        state, kind = row["status"], row["kind"]
        actions = []
        if row["owner"] == p.user:
            if state in {"submitted", "pending_approval"}: actions.append("cancel")
            if state == "resolved": actions += ["accept", "reopen"]
        if p.role == "support":
            if kind == "access_request":
                if state == "pending_approval" and row["owner"] != p.user: actions += ["approve", "reject"]
                if state == "approved" and row["owner"] != p.user: actions.append("provision")
            else:
                actions += {"submitted": ["triage"], "triaged": ["assign"], "assigned": ["start"], "in_progress": ["resolve"]}.get(state, [])
        return actions

    def get(self, p, ticket_id):
        with self.store.db() as db: return self._public(db, p, self._get(db, p, ticket_id))

    def list(self, p):
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM service_cases WHERE tenant=?" + ("" if p.role == "support" else " AND owner=?") + " ORDER BY updated DESC LIMIT 100",
                              [p.tenant] + ([] if p.role == "support" else [p.user])).fetchall()
            return [self._public(db, p, r) for r in rows]

    def transition(self, p, ticket_id, action, version, request_key, note="", assignee=""):
        if not note.strip(): raise ValueError("请填写处理或验收说明")
        fingerprint = digest([action, version, note, assignee])
        with self.store.db(True) as db:
            row = self._get(db, p, ticket_id)
            previous = db.execute("SELECT fingerprint,response FROM service_actions WHERE tenant=? AND actor=? AND ticket_id=? AND request_key=?",
                                  (p.tenant, p.user, ticket_id, request_key)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint: raise Conflict("幂等键已经用于其他操作")
                return json.loads(previous["response"])
            if row["version"] != version: raise Conflict("流程已更新，请刷新后操作")
            if action not in self.allowed(p, row): raise Conflict("当前状态或角色不允许此操作")
            fields = json.loads(row["fields"])
            targets = {"approve": "approved", "reject": "rejected", "provision": "fulfilled", "cancel": "cancelled",
                       "triage": "triaged", "assign": "assigned", "start": "in_progress", "resolve": "resolved", "accept": "closed", "reopen": "in_progress"}
            target = targets[action]
            if action == "assign":
                # Local demo has one support operator; no invented external employee identity.
                if assignee != p.user: raise ValueError("本地模拟仅支持分派给当前 IT 操作员")
            if action in {"start", "resolve"} and row["assignee"] != p.user:
                raise PermissionError("只有被分派的 IT 操作员可以处理")
            if action == "provision":
                missing = self.validate(db, p, fields)
                if missing: raise ValueError("申请不完整")
                db.execute("INSERT INTO desk_grants VALUES(?,?,?,?,?,?) ON CONFLICT(tenant,owner,resource) DO UPDATE SET level=excluded.level,expires=excluded.expires,source_ticket=excluded.source_ticket",
                           (p.tenant, row["owner"], fields["resource_id"], fields["access_level"], time.time()+fields["duration_days"]*86400, ticket_id))
            if row["kind"] == "device_repair" and action in {"accept", "cancel"}:
                db.execute("UPDATE desk_assets SET status='available' WHERE tenant=? AND id=?", (p.tenant, fields["asset_id"]))
            db.execute("UPDATE service_cases SET status=?,version=version+1,assignee=?,updated=? WHERE ticket_id=?",
                       (target, assignee if action == "assign" else row["assignee"], time.time(), ticket_id))
            db.execute("UPDATE tickets SET status=? WHERE id=? AND tenant=?", (target, ticket_id, p.tenant))
            self._audit(db, p, ticket_id, action, row["status"], target, note)
            result = self._public(db, p, self._get(db, p, ticket_id))
            db.execute("INSERT INTO service_actions VALUES(?,?,?,?,?,?)", (p.tenant, p.user, ticket_id, request_key, fingerprint, encode(result)))
            return result
