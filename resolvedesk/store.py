"""Transactional runs, events, approvals, tickets and feedback in one SQLite DB."""
from __future__ import annotations

from contextlib import contextmanager
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


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        for ancestor in (self.path, *self.path.parents):
            if ancestor.is_symlink() or ancestor.is_junction():
                raise ValueError("Database paths must not follow links")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
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
            """)
            from resolvedesk.business import initialize
            initialize(db)

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if db.in_transaction:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db, run_id, kind, payload):
        db.execute("INSERT INTO events(run_id,kind,payload,created) VALUES(?,?,?,?)",
                   (run_id, kind, encode(payload), time.time()))

    @staticmethod
    def _run(db, principal, run_id):
        # Runs and confirmations always belong to the requester, even for support staff.
        row = db.execute("SELECT * FROM runs WHERE id=? AND tenant=? AND owner=?",
                         (run_id, principal.tenant, principal.user)).fetchone()
        if row is None:
            raise KeyError("Run not found")
        return row

    @staticmethod
    def _public(row):
        item = dict(row)
        item["draft"] = json.loads(item["draft"]) if item["draft"] else None
        if item["status"] != "awaiting_confirmation":
            item.pop("approval", None)
        return item

    def create_run(self, p, message, request_key):
        with self.db(True) as db:
            row = db.execute("SELECT * FROM runs WHERE tenant=? AND owner=? AND request_key=?",
                             (p.tenant, p.user, request_key)).fetchone()
            if row:
                if row["message"] != message:
                    raise Conflict("Idempotency key already used with a different message")
                return self._public(row), False
            run_id, now = uuid.uuid4().hex, time.time()
            db.execute("INSERT INTO runs(id,tenant,owner,request_key,message,status,created,updated) VALUES(?,?,?,?,?,'queued',?,?)",
                       (run_id, p.tenant, p.user, request_key, message, now, now))
            db.execute("INSERT INTO messages(run_id,role,content) VALUES(?,'user',?)", (run_id, message))
            self._event(db, run_id, "queued", {})
            return self._public(self._run(db, p, run_id)), True

    def get_run(self, p, run_id):
        with self.db() as db:
            result=self._public(self._run(db,p,run_id))
            result['event_cursor']=db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE run_id=? AND kind='queued'",(run_id,)).fetchone()[0]
            return result

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
        with self.db(True) as db:
            row = self._run(db, p, run_id)
            if row["status"] != "queued":
                return False
            db.execute("UPDATE runs SET status='running',updated=? WHERE id=?", (time.time(), run_id))
            self._event(db, run_id, "running", {})
            return True

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

    def reply(self, p, run_id, message):
        with self.db(True) as db:
            row = self._run(db, p, run_id)
            if row["status"] not in {"awaiting_input", "awaiting_confirmation", "completed"} or row["ticket_id"]:
                raise Conflict("Run is not waiting for input")
            db.execute("INSERT INTO messages(run_id,role,content) VALUES(?,'user',?)", (run_id, message))
            # Any draft revision invalidates the old approval immediately.
            db.execute("UPDATE runs SET status='queued',approval=NULL,expires=NULL,version=version+1,updated=? WHERE id=?",
                       (time.time(), run_id))
            self._event(db, run_id, "queued", {"reason": "user_reply"})

    def prepare(self, p, run_id, fields, missing):
        with self.db(True) as db:
            row = self._run(db, p, run_id)
            if row["status"] != "running":
                raise Conflict("Run is no longer running")
            version = row["version"] + 1
            approval = digest([run_id, version, fields]) if not missing else None
            status = "awaiting_input" if missing else "awaiting_confirmation"
            answer = ("创建工单还需要：" + "、".join(missing)) if missing else "工单草稿已准备好，请核对内容并确认提交。"
            db.execute("UPDATE runs SET status=?,draft=?,version=?,approval=?,expires=?,answer=?,updated=? WHERE id=?",
                       (status, encode(fields), version, approval, time.time()+900 if approval else None, answer, time.time(), run_id))
            db.execute("INSERT INTO messages(run_id,role,content) VALUES(?,'assistant',?)", (run_id, answer))
            self._event(db, run_id, status, {"draft": fields, "missing": missing, "version": version})

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
            db.execute("UPDATE runs SET status='paused',updated=? WHERE id=?", (time.time(), run_id))
            self._event(db, run_id, "paused", {"reason": "user"})

    def resume(self, p, run_id):
        with self.db(True) as db:
            row = self._run(db, p, run_id)
            if row["status"] not in {"paused", "failed"}:
                raise Conflict("Run cannot be resumed in this state")
            db.execute("UPDATE runs SET status='queued',error=NULL,updated=? WHERE id=?", (time.time(), run_id))
            self._event(db, run_id, "queued", {"reason": "explicit_resume"})

    def recover(self):
        # One local worker process owns this DB. No automatic model replay on restart.
        with self.db(True) as db:
            rows = db.execute("SELECT id FROM runs WHERE status IN ('running','queued')").fetchall()
            for row in rows:
                db.execute("UPDATE runs SET status='paused',error='process_interrupted',updated=? WHERE id=?", (time.time(), row["id"]))
                self._event(db, row["id"], "paused", {"reason": "process_interrupted"})
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
