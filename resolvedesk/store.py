"""Transactional runs, receipts and fenced execution in PostgreSQL or offline SQLite."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


@dataclass(frozen=True)
class Principal:
    tenant: str
    user: str
    role: str = "user"


class Conflict(ValueError):
    pass


class FencedExecution(Conflict):
    pass


class ReceiptExpired(Conflict):
    pass


@dataclass(frozen=True)
class ExecutionToken:
    run_id: str
    owner: str
    epoch: int


_execution = ContextVar("resolvedesk_execution", default=None)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class Store:
    def __init__(self, path, *, schema="public"):
        from resolvedesk.database import Database
        self.database = Database(path, schema)
        self.path = self.database.path
        self.backend = self.database.backend
        with self.database.schema_transaction() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY, tenant TEXT NOT NULL, owner TEXT NOT NULL,
                request_key TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '', draft TEXT, version INTEGER NOT NULL DEFAULT 0,
                approval TEXT, expires REAL, ticket_id TEXT, error TEXT,
                created REAL NOT NULL, updated REAL NOT NULL,
                UNIQUE(tenant,owner,request_key));
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS event_run ON events(run_id,seq);
            CREATE TABLE IF NOT EXISTS messages(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tickets(
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, tenant TEXT NOT NULL,
                owner TEXT NOT NULL, fields TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS feedback(
                run_id TEXT NOT NULL, tenant TEXT NOT NULL, owner TEXT NOT NULL,
                rating TEXT NOT NULL, note TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(run_id,tenant,owner));
            CREATE TABLE IF NOT EXISTS checkpoints(
                run_id TEXT PRIMARY KEY, tenant TEXT, owner TEXT, user_seq INTEGER, payload TEXT);
            CREATE TABLE IF NOT EXISTS turn_receipts(
                tenant TEXT,owner TEXT,run_id TEXT,request_key TEXT,input_hash TEXT,user_seq INTEGER,
                status TEXT,response TEXT,created REAL,updated REAL,
                PRIMARY KEY(tenant,owner,run_id,request_key));
            """)
            for column, declaration in (("execution_owner","TEXT"),("execution_epoch","INTEGER NOT NULL DEFAULT 0"),("lease_until","DOUBLE PRECISION NOT NULL DEFAULT 0")):
                if not db.has_column("runs",column): db.execute("ALTER TABLE runs ADD COLUMN "+column+" "+declaration)
            from resolvedesk.business import initialize
            initialize(db)
            from resolvedesk.metrics import initialize as initialize_metrics
            initialize_metrics(db)

    @contextmanager
    def db(self, write=False):
        with self.database.connect(write) as db:
            yield db

    def has_column(self, table, column):
        return self.database.has_column(table, column)

    def close(self):
        self.database.close()

    @contextmanager
    def execution(self, token):
        marker = _execution.set(token)
        try: yield
        finally: _execution.reset(marker)

    @staticmethod
    def _event(db, run_id, kind, payload):
        db.execute("INSERT INTO events(run_id,kind,payload,created) VALUES(?,?,?,?)",
                   (run_id, kind, encode(payload), time.time()))

    @staticmethod
    def _run(db, principal, run_id):
        # Runs and confirmations always belong to the requester, even for support staff.
        row = db.execute(db.locked("SELECT * FROM runs WHERE id=? AND tenant=? AND owner=?"),
                         (run_id, principal.tenant, principal.user)).fetchone()
        if row is None:
            raise KeyError("Run not found")
        token = _execution.get()
        if token and token.run_id == run_id and db.write and row["status"] == "running":
            if row["execution_owner"] != token.owner or row["execution_epoch"] != token.epoch or row["lease_until"] <= db.now():
                raise FencedExecution("Execution ownership expired or changed")
        return row

    @staticmethod
    def _public(row):
        item = dict(row)
        item["draft"] = json.loads(item["draft"]) if item["draft"] else None
        for key in ("execution_owner","execution_epoch","lease_until"): item.pop(key,None)
        if item["status"] != "awaiting_confirmation":
            item.pop("approval", None)
        return item

    @staticmethod
    def _snapshot(db,row):
        result = Store._public(row)
        result["event_cursor"] = db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE run_id=? AND kind='queued'",(row["id"],)).fetchone()[0]
        result["event_end"] = db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE run_id=?",(row["id"],)).fetchone()[0]
        receipt = db.execute("SELECT request_key,user_seq FROM turn_receipts WHERE run_id=? ORDER BY user_seq DESC LIMIT 1",(row["id"],)).fetchone()
        if receipt: result.update(turn_key=receipt["request_key"],turn_seq=receipt["user_seq"])
        return result

    @staticmethod
    def _receipt(db,row):
        seq = db.execute("SELECT MAX(seq) FROM messages WHERE run_id=? AND role='user'",(row["id"],)).fetchone()[0]
        from resolvedesk.metrics import finish_attempt
        # Observability failure must not undo a completed answer/approval state.
        db.execute('SAVEPOINT attempt_metrics')
        try:finish_attempt(db,row)
        except Exception as exc:
            db.execute('ROLLBACK TO SAVEPOINT attempt_metrics')
            Store._event(db,row['id'],'monitor_error',{'type':type(exc).__name__})
        finally:db.execute('RELEASE SAVEPOINT attempt_metrics')
        db.execute("UPDATE turn_receipts SET status=?,response=?,updated=? WHERE run_id=? AND user_seq=?",
                   (row["status"],encode(Store._snapshot(db,row)),time.time(),row["id"],seq))

    def create_run(self, p, message, request_key):
        with self.db(True) as db:
            run_id, now = uuid.uuid4().hex, time.time()
            inserted = db.execute("INSERT INTO runs(id,tenant,owner,request_key,message,status,created,updated) VALUES(?,?,?,?,?,'queued',?,?) ON CONFLICT(tenant,owner,request_key) DO NOTHING",
                       (run_id, p.tenant, p.user, request_key, message, now, now))
            if not inserted.rowcount:
                row = db.execute("SELECT * FROM runs WHERE tenant=? AND owner=? AND request_key=?",
                             (p.tenant, p.user, request_key)).fetchone()
                if row["message"] != message:
                    raise Conflict("Idempotency key already used with a different message")
                return self._turn(db,p,row["id"],request_key), False
            db.execute("INSERT INTO messages(run_id,role,content) VALUES(?,'user',?)", (run_id, message))
            self._event(db, run_id, "queued", {})
            seq = db.execute("SELECT MAX(seq) FROM messages WHERE run_id=?",(run_id,)).fetchone()[0]
            db.execute("INSERT INTO turn_receipts VALUES(?,?,?,?,?,?,'queued',NULL,?,?)",(p.tenant,p.user,run_id,request_key,digest(["create",message]),seq,now,now))
            self._receipt(db,self._run(db,p,run_id))
            return self._snapshot(db,self._run(db, p, run_id)), True

    def get_run(self, p, run_id):
        with self.db() as db:
            return self._snapshot(db,self._run(db,p,run_id))

    @staticmethod
    def _turn(db,p,run_id,request_key):
        row = db.execute("SELECT response FROM turn_receipts WHERE tenant=? AND owner=? AND run_id=? AND request_key=?",(p.tenant,p.user,run_id,request_key)).fetchone()
        if row is None: raise KeyError("Turn not found")
        if row["response"] is None: raise ReceiptExpired("Turn evidence expired; this request key cannot be executed again")
        return json.loads(row["response"])

    def get_turn(self,p,run_id,request_key):
        with self.db() as db:
            self._run(db,p,run_id)
            return self._turn(db,p,run_id,request_key)

    def events(self, p, run_id, after=0):
        with self.db() as db:
            self._run(db, p, run_id)
            return [dict(row, payload=json.loads(row["payload"])) for row in db.execute(
                "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 200", (run_id, after))]

    def event(self, p, run_id, kind, payload):
        with self.db(True) as db:
            row = self._run(db, p, run_id)
            if row["status"] != "running":
                raise Conflict("Run is no longer running")
            self._event(db, run_id, kind, payload)

    def claim(self, p, run_id):
        return self.claim_execution(p,run_id,uuid.uuid4().hex) is not None

    def claim_execution(self,p,run_id,owner,lease_seconds=120,*,instance='unknown'):
        with self.db(True) as db:
            row = self._run(db, p, run_id)
            if row["status"] != "queued":
                return None
            epoch = row["execution_epoch"]+1
            now=db.now()
            turn=db.execute('SELECT request_key,user_seq FROM turn_receipts WHERE run_id=? ORDER BY user_seq DESC LIMIT 1',(run_id,)).fetchone()
            db.execute('INSERT INTO execution_attempts VALUES(?,?,?,?,?,?,?,?,?,NULL,\'running\',NULL)',
                (run_id,epoch,p.tenant,p.user,turn['request_key'] if turn else '',turn['user_seq'] if turn else 0,instance,now,now+lease_seconds))
            db.execute("UPDATE runs SET status='running',execution_owner=?,execution_epoch=?,lease_until=?,updated=? WHERE id=?", (owner,epoch,now+lease_seconds,now,run_id))
            self._event(db, run_id, "running", {"epoch":epoch})
            self._receipt(db,self._run(db,p,run_id))
            return ExecutionToken(run_id,owner,epoch)

    def renew_execution(self,p,token,lease_seconds=120):
        with self.db(True) as db:
            now=db.now()
            updated = db.execute("UPDATE runs SET lease_until=? WHERE id=? AND tenant=? AND owner=? AND status='running' AND execution_owner=? AND execution_epoch=? AND lease_until>?",
                (now+lease_seconds,token.run_id,p.tenant,p.user,token.owner,token.epoch,now))
            if updated.rowcount:db.execute('UPDATE execution_attempts SET lease_until=? WHERE run_id=? AND epoch=?',(now+lease_seconds,token.run_id,token.epoch))
            return updated.rowcount==1

    def lose_execution(self,p,token,reason="lease_lost"):
        with self.db(True) as db:
            updated = db.execute("UPDATE runs SET status='paused',execution_owner=NULL,lease_until=0,error=?,updated=? WHERE id=? AND tenant=? AND owner=? AND status='running' AND execution_owner=? AND execution_epoch=?",
                (reason,time.time(),token.run_id,p.tenant,p.user,token.owner,token.epoch))
            if updated.rowcount:
                self._event(db,token.run_id,"paused",{"reason":reason})
                self._receipt(db,self._run(db,p,token.run_id))
            return bool(updated.rowcount)

    def finish(self, p, run_id, status, answer, error=None):
        if status not in {"completed", "awaiting_input", "failed", "paused"}:
            raise ValueError("Invalid terminal status")
        with self.db(True) as db:
            row = self._run(db, p, run_id)
            if row["status"] != "running":
                raise Conflict("Run is no longer running")
            db.execute("UPDATE runs SET status=?,answer=?,error=?,updated=? WHERE id=?",
                       (status, answer, error, time.time(), run_id))
            if answer:
                db.execute("INSERT INTO messages(run_id,role,content) VALUES(?,'assistant',?)", (run_id, answer))
            self._event(db, run_id, status, {"answer": answer, "error": error})
            self._receipt(db,self._run(db,p,run_id))

    def history(self, p, run_id):
        with self.db() as db:
            self._run(db, p, run_id)
            rows = db.execute("SELECT role,content FROM messages WHERE run_id=? ORDER BY seq DESC LIMIT 12", (run_id,)).fetchall()
            return [dict(row) for row in reversed(rows)]

    def latest_user_seq(self,p,run_id):
        with self.db() as db:
            self._run(db,p,run_id)
            return db.execute("SELECT MAX(seq) FROM messages WHERE run_id=? AND role='user'",(run_id,)).fetchone()[0]

    def checkpoint(self,p,run_id,payload):
        with self.db(True) as db:
            row = self._run(db,p,run_id)
            if row["status"] != "running":raise Conflict("Run is no longer running")
            seq = db.execute("SELECT MAX(seq) FROM messages WHERE run_id=? AND role='user'",(run_id,)).fetchone()[0]
            db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET user_seq=excluded.user_seq,payload=excluded.payload",(run_id,p.tenant,p.user,seq,encode(payload)))

    def load_checkpoint(self,p,run_id):
        with self.db() as db:
            self._run(db,p,run_id)
            row = db.execute("SELECT * FROM checkpoints WHERE run_id=? AND tenant=? AND owner=?",(run_id,p.tenant,p.user)).fetchone()
            seq = db.execute("SELECT MAX(seq) FROM messages WHERE run_id=? AND role='user'",(run_id,)).fetchone()[0]
            return json.loads(row["payload"]) if row and row["user_seq"] == seq else None

    def list_runs(self,p):
        with self.db() as db:
            return [self._public(row) for row in db.execute("SELECT * FROM runs WHERE tenant=? AND owner=? ORDER BY updated DESC LIMIT 100",(p.tenant,p.user))]

    def reply(self, p, run_id, message, request_key=None):
        request_key = request_key or "internal-"+uuid.uuid4().hex
        with self.db(True) as db:
            row = self._run(db, p, run_id)
            previous = db.execute("SELECT * FROM turn_receipts WHERE tenant=? AND owner=? AND run_id=? AND request_key=?",(p.tenant,p.user,run_id,request_key)).fetchone()
            if previous:
                if previous["input_hash"] != digest(["reply",message]): raise Conflict("Idempotency key already used with a different message")
                return self._turn(db,p,run_id,request_key),False
            if row["status"] not in {"awaiting_input", "awaiting_confirmation", "completed"} or row["ticket_id"]:
                raise Conflict("Run is not waiting for input")
            db.execute("INSERT INTO messages(run_id,role,content) VALUES(?,'user',?)", (run_id, message))
            # Any draft revision invalidates the old approval immediately.
            db.execute("UPDATE runs SET status='queued',answer='',error=NULL,approval=NULL,expires=NULL,version=version+1,execution_owner=NULL,lease_until=0,updated=? WHERE id=?",
                       (time.time(), run_id))
            self._event(db, run_id, "queued", {"reason": "user_reply"})
            seq = db.execute("SELECT MAX(seq) FROM messages WHERE run_id=?",(run_id,)).fetchone()[0]
            now = time.time()
            db.execute("INSERT INTO turn_receipts VALUES(?,?,?,?,?,?,'queued',NULL,?,?)",(p.tenant,p.user,run_id,request_key,digest(["reply",message]),seq,now,now))
            row = self._run(db,p,run_id)
            self._receipt(db,row)
            return self._snapshot(db,row),True

    def prepare(self, p, run_id, fields, missing):
        with self.db(True) as db:
            row = self._run(db, p, run_id)
            if row["status"] != "running":
                raise Conflict("Run is no longer running")
            version = row["version"] + 1
            approval = digest([run_id, version, fields]) if not missing else None
            status = "awaiting_input" if missing else "awaiting_confirmation"
            answer = ("创建工单还需要：" + "、".join(missing)) if missing else "工单草稿已准备好，请核对内容并确认提交。"
            if not missing and fields.get("workflow") == "access_request":
                answer = (f"权限申请草稿已准备：{fields['summary']}。\n资源：{fields['resource_id']}；权限：{fields['access_level']}；有效期：{fields['duration_days']} 天。\n"
                          f"用途与说明：{fields['description']}\n请核对后点击确认提交。确认后进入待审批，须由其他 IT 客服审核；批准后仍需执行模拟开通。"
                          "有效期从模拟开通时起算，所有操作保留审计记录。目前尚未提交申请或开通权限；本流程只更改本地模拟系统。")
            elif not missing and fields.get("workflow") == "device_repair":
                answer = (f"设备维修草稿已准备：{fields['summary']}。\n资产：{fields['asset_id']}；故障：{fields['description']}\n"
                          "请核对后点击确认提交。提交后由 IT 分诊、派单并记录维修处理结果，再由你验收关闭；未解决可退回返工。"
                          "尚未创建维修工单或完成维修，不承诺维修时效与费用；本流程使用本地模拟资产和维修记录。")
            db.execute("UPDATE runs SET status=?,draft=?,version=?,approval=?,expires=?,answer=?,updated=? WHERE id=?",
                       (status, encode(fields), version, approval, time.time()+900 if approval else None, answer, time.time(), run_id))
            db.execute("INSERT INTO messages(run_id,role,content) VALUES(?,'assistant',?)", (run_id, answer))
            self._event(db, run_id, status, {"draft": fields, "missing": missing, "version": version})
            self._receipt(db,self._run(db,p,run_id))

    def clear_draft(self,p,run_id):
        with self.db(True) as db:
            row=self._run(db,p,run_id)
            if row["status"]!="running":raise Conflict("Run is no longer running")
            db.execute("UPDATE runs SET draft=NULL,approval=NULL,expires=NULL,version=version+1 WHERE id=?",(run_id,))
            self._event(db,run_id,"task_switch",{"draft_reset":True})

    def confirm(self, p, run_id, approval):
        with self.db(True) as db:
            row = self._run(db, p, run_id)
            if row["status"] == "completed" and row["ticket_id"] and row["approval"] == approval:
                return self._ticket(db, p, row["ticket_id"])
            if row["status"] != "awaiting_confirmation" or row["approval"] != approval:
                raise Conflict("Approval does not match the current draft")
            if row["expires"] < time.time():
                raise Conflict("Approval expired; revise or repeat the request")
            fields = json.loads(row["draft"])
            if digest([run_id, row["version"], fields]) != approval:
                raise Conflict("Draft changed after approval was issued")
            ticket_id = "RD-" + uuid.uuid4().hex[:12].upper()
            db.execute("INSERT INTO tickets VALUES(?,?,?,?,?,'open',?)",
                       (ticket_id, run_id, p.tenant, p.user, row["draft"], time.time()))
            from resolvedesk.business import BusinessService
            BusinessService.materialize(db, p, ticket_id, fields)
            answer = "工单已创建：" + ticket_id
            db.execute("UPDATE runs SET status='completed',ticket_id=?,answer=?,updated=? WHERE id=?",
                       (ticket_id, answer, time.time(), run_id))
            self._event(db, run_id, "ticket_created", {"ticket_id": ticket_id})
            self._event(db, run_id, "completed", {"answer": answer})
            self._receipt(db,self._run(db,p,run_id))
            return self._ticket(db, p, ticket_id)

    @staticmethod
    def _ticket(db, p, ticket_id):
        clause, params = ("", []) if p.role == "support" else (" AND owner=?", [p.user])
        row = db.execute("SELECT * FROM tickets WHERE id=? AND tenant=?"+clause,
                         [ticket_id, p.tenant]+params).fetchone()
        if row is None:
            raise KeyError("Ticket not found")
        return dict(row, fields=json.loads(row["fields"]))

    def get_ticket(self, p, ticket_id):
        with self.db() as db:
            return self._ticket(db, p, ticket_id)

    def list_tickets(self, p):
        with self.db() as db:
            clause, params = ("", []) if p.role == "support" else (" AND owner=?", [p.user])
            rows = db.execute("SELECT id FROM tickets WHERE tenant=?"+clause+" ORDER BY created DESC LIMIT 100",
                              [p.tenant]+params).fetchall()
            return [self._ticket(db, p, row["id"]) for row in rows]

    def pause(self, p, run_id):
        with self.db(True) as db:
            row = self._run(db, p, run_id)
            if row["status"] not in {"queued", "running"}:
                raise Conflict("Only active runs can be paused")
            db.execute("UPDATE runs SET status='paused',execution_owner=NULL,lease_until=0,updated=? WHERE id=?", (time.time(), run_id))
            self._event(db, run_id, "paused", {"reason": "user"})
            self._receipt(db,self._run(db,p,run_id))

    def resume(self, p, run_id):
        with self.db(True) as db:
            row = self._run(db, p, run_id)
            if row["status"] not in {"paused", "failed"}:
                raise Conflict("Run cannot be resumed in this state")
            db.execute("UPDATE runs SET status='queued',error=NULL,updated=? WHERE id=?", (time.time(), run_id))
            self._event(db, run_id, "queued", {"reason": "explicit_resume"})
            self._receipt(db,self._run(db,p,run_id))

    def recover_expired(self):
        """Service mode: do not pause another live worker's active execution."""
        with self.db(True) as db:
            now=db.now()
            rows = db.execute(db.locked("SELECT id,tenant,owner FROM runs WHERE (status='running' AND lease_until<=?) OR (status='queued' AND updated<?)"),(now,now-180)).fetchall()
            for row in rows:
                db.execute("UPDATE runs SET status='paused',execution_owner=NULL,lease_until=0,error='execution_interrupted',updated=? WHERE id=?",(now,row["id"]))
                self._event(db,row["id"],"paused",{"reason":"execution_interrupted"})
                self._receipt(db,self._run(db,Principal(row["tenant"],row["owner"]),row["id"]))
            return len(rows)

    def recover(self):
        # One local worker process owns this DB. No automatic model replay on restart.
        with self.db(True) as db:
            rows = db.execute("SELECT id FROM runs WHERE status IN ('running','queued')").fetchall()
            for row in rows:
                db.execute("UPDATE runs SET status='paused',error='process_interrupted',updated=? WHERE id=?", (time.time(), row["id"]))
                self._event(db, row["id"], "paused", {"reason": "process_interrupted"})
                current=db.execute('SELECT * FROM runs WHERE id=?',(row['id'],)).fetchone()
                self._receipt(db,current)
            return len(rows)

    def feedback(self, p, run_id, rating, note):
        with self.db(True) as db:
            self._run(db, p, run_id)
            db.execute("INSERT INTO feedback VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,tenant,owner) DO UPDATE SET rating=excluded.rating,note=excluded.note,created=excluded.created",
                       (run_id, p.tenant, p.user, rating, note, time.time()))

    def regression_candidates(self, p):
        if p.role != "support":
            raise PermissionError("Support role required")
        with self.db() as db:
            # Candidate fixtures require human review; never automatically alter eval baselines.
            return [dict(row) for row in db.execute("""SELECT f.run_id,r.message AS question,r.answer,
                f.rating,f.note FROM feedback f JOIN runs r ON f.run_id=r.id
                WHERE f.tenant=? AND f.rating='bad' ORDER BY f.created DESC LIMIT 100""", (p.tenant,))]
