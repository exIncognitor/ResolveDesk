"""Persistent rolling metrics, alert lifecycle and bounded webhook outbox."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import httpx

from resolvedesk.store import encode


class Monitor:
    def __init__(self, store, window=300, min_samples=3):
        self.store, self.window, self.min_samples = store, window, min_samples
        self.task = None
        with store.database.schema_transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS telemetry(seq INTEGER PRIMARY KEY AUTOINCREMENT,tenant TEXT,kind TEXT,
                    name TEXT,success INTEGER,seconds REAL,created REAL);
                CREATE INDEX IF NOT EXISTS telemetry_window ON telemetry(tenant,created);
                CREATE TABLE IF NOT EXISTS desk_alerts(id TEXT PRIMARY KEY,tenant TEXT,rule_key TEXT,state TEXT,
                    payload TEXT,created REAL,updated REAL,UNIQUE(tenant,rule_key));
                CREATE TABLE IF NOT EXISTS alert_outbox(id TEXT PRIMARY KEY,tenant TEXT,alert_id TEXT,event TEXT,
                    payload TEXT,state TEXT,attempts INTEGER,next_attempt REAL,error TEXT,created REAL);
            """)

    def record(self, tenant, kind, name, success, seconds=0):
        with self.store.db(True) as db:
            db.execute("INSERT INTO telemetry(tenant,kind,name,success,seconds,created) VALUES(?,?,?,?,?,?)",
                       (tenant, kind, name, int(success), max(0, seconds), time.time()))

    def snapshot(self, p):
        return self.refresh(p.tenant)

    def refresh(self, tenant):
        now = time.time()
        with self.store.db(True) as db:
            rows = db.execute("SELECT kind,name,success,seconds FROM telemetry WHERE tenant=? AND created>=? ORDER BY seq DESC LIMIT 10000", (tenant, now-self.window)).fetchall()
            buckets = {}
            for row in rows: buckets.setdefault((row["kind"], row["name"]), []).append(row)
            groups = {"agent": {}, "tool": {}, "run": {}, "retrieval": {}}
            triggered = {}
            for (kind, name), samples in buckets.items():
                latencies = sorted(r["seconds"] for r in samples)
                n, success = len(samples), sum(r["success"] for r in samples)
                stats = {"total": n, "success": success, "success_rate": success/n,
                         "avg_ms": sum(latencies)/n*1000, "p95_ms": latencies[max(0, (n*95+99)//100-1)]*1000}
                groups.setdefault(kind, {})[name] = stats
                if n >= self.min_samples:
                    if stats["success_rate"] < .5:
                        triggered[f"{kind}:{name}:failure"] = {"kind": kind, "name": name, "rule": "failure_rate", "message": "最近窗口失败率超过 50%", "stats": stats}
                    if stats["p95_ms"] > 30000:
                        triggered[f"{kind}:{name}:latency"] = {"kind": kind, "name": name, "rule": "latency", "message": "最近窗口 P95 超过 30 秒", "stats": stats}
            existing = {r["rule_key"]: r for r in db.execute("SELECT * FROM desk_alerts WHERE tenant=?", (tenant,))}
            for key in existing.keys() | triggered.keys():
                previous = existing.get(key)
                state = "active" if key in triggered else "resolved"
                payload = triggered.get(key) or json.loads(previous["payload"])
                if previous is None or previous["state"] != state:
                    alert_id = previous["id"] if previous else uuid.uuid4().hex
                    db.execute("INSERT INTO desk_alerts VALUES(?,?,?,?,?,?,?) ON CONFLICT(tenant,rule_key) DO UPDATE SET state=excluded.state,payload=excluded.payload,updated=excluded.updated",
                               (alert_id, tenant, key, state, encode(payload), now, now))
                    event = {"alert_id": alert_id, "state": state, "rule_key": key, **payload}
                    db.execute("INSERT INTO alert_outbox VALUES(?,?,?,?,?,'pending',0,?,NULL,?)", (uuid.uuid4().hex, tenant, alert_id, state, encode(event), now, now))
                elif state == "active":
                    db.execute("UPDATE desk_alerts SET payload=?,updated=? WHERE tenant=? AND rule_key=?", (encode(payload), now, tenant, key))
            alerts = [dict(r, payload=json.loads(r["payload"])) for r in db.execute("SELECT * FROM desk_alerts WHERE tenant=? AND state='active'", (tenant,))]
            count = db.execute("SELECT COUNT(*) FROM runs WHERE tenant=?", (tenant,)).fetchone()[0]
            outbox = [dict(r) for r in db.execute("SELECT event,state,attempts,error,created FROM alert_outbox WHERE tenant=? ORDER BY created DESC LIMIT 20", (tenant,))]
        penalties = {a["payload"]["name"]: .9 for a in alerts if a["payload"]["kind"] == "agent" and a["payload"]["rule"] == "failure_rate"}
        return {"agent_stats": groups["agent"], "tool_stats": groups["tool"], "run_stats": groups["run"],
                "retrieval_stats": groups["retrieval"], "active_alerts": alerts, "routing_penalties": penalties,
                "suggestions": ["异常专业节点已降权；请求仍需遵守权限与确认规则。"] if penalties else [],
                "total_requests": count, "window_seconds": self.window, "min_samples": self.min_samples,
                "notifications": outbox, "webhook_configured": bool(os.getenv("RESOLVEDESK_ALERT_WEBHOOK"))}

    async def deliver(self):
        endpoint = os.getenv("RESOLVEDESK_ALERT_WEBHOOK", "")
        # No destination configured: preserve events for UI/local inspection, never send externally.
        if not endpoint: return
        with self.store.db() as db:
            messages = [dict(r) for r in db.execute("SELECT * FROM alert_outbox WHERE state='pending' AND next_attempt<=? ORDER BY created LIMIT 10", (time.time(),))]
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            for message in messages:
                try:
                    response = await client.post(endpoint, json=json.loads(message["payload"]), headers={"Idempotency-Key": message["id"]})
                    response.raise_for_status()
                    state, error = "delivered", None
                except Exception as exc:
                    state, error = ("failed" if message["attempts"] >= 2 else "pending"), type(exc).__name__
                with self.store.db(True) as db:
                    db.execute("UPDATE alert_outbox SET state=?,attempts=attempts+1,next_attempt=?,error=? WHERE id=?", (state, time.time()+30, error, message["id"]))

    async def _loop(self):
        while True:
            try:
                with self.store.db() as db:
                    tenants = [r[0] for r in db.execute("SELECT DISTINCT tenant FROM telemetry UNION SELECT DISTINCT tenant FROM desk_alerts WHERE state='active'")]
                for tenant in tenants: self.refresh(tenant)
                await self.deliver()
            except asyncio.CancelledError: raise
            except Exception:
                # A monitor outage must not terminate request execution; retry next interval.
                pass
            await asyncio.sleep(5)

    def start(self):
        if self.task is None: self.task = asyncio.create_task(self._loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def prometheus(self, p):
        snapshot = self.snapshot(p)
        lines = ["# TYPE resolvedesk_requests_total counter", f"resolvedesk_requests_total {snapshot['total_requests']}",
                 "# TYPE resolvedesk_active_alerts gauge", f"resolvedesk_active_alerts {len(snapshot['active_alerts'])}"]
        for kind in ("agent", "tool", "run", "retrieval"):
            for name, stats in snapshot[kind+"_stats"].items():
                labels = f'kind={json.dumps(kind)},name={json.dumps(name)}'
                for metric in ("total", "success", "p95_ms"):
                    lines.append(f"resolvedesk_window_{metric}{{{labels}}} {stats[metric]}")
        return "\n".join(lines)+"\n"
