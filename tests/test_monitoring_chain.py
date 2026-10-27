import httpx
import pytest

from resolvedesk.monitoring import Monitor
from resolvedesk.store import Store, Principal


def test_alert_trigger_dedup_recovery_is_persistent_and_scoped(tmp_path):
    store = Store(tmp_path/"monitor.sqlite3")
    monitor = Monitor(store)
    p = Principal("demo", "support", "support")
    for _ in range(3): monitor.record("demo", "agent", "technical", False, .1)
    snapshot = monitor.snapshot(p)
    assert len(snapshot["active_alerts"]) == 1 and snapshot["routing_penalties"] == {"technical": .9}
    assert len(monitor.snapshot(p)["notifications"]) == 1
    assert not monitor.snapshot(Principal("other", "support", "support"))["active_alerts"]
    for _ in range(4): monitor.record("demo", "agent", "technical", True, .1)
    recovered = Monitor(Store(store.path)).snapshot(p)
    assert not recovered["active_alerts"] and not recovered["routing_penalties"]
    assert len(recovered["notifications"]) == 2
    assert {n["event"] for n in recovered["notifications"]} == {"active", "resolved"}


@pytest.mark.asyncio
async def test_webhook_retries_and_delivery_use_outbox_without_secrets(tmp_path, monkeypatch):
    monitor = Monitor(Store(tmp_path/"outbox.sqlite3"))
    for _ in range(3): monitor.record("demo", "tool", "knowledge_search", False)
    monitor.refresh("demo")
    # No endpoint means no delivery attempt.
    monkeypatch.delenv("RESOLVEDESK_ALERT_WEBHOOK", raising=False)
    await monitor.deliver()
    assert monitor.refresh("demo")["notifications"][0]["attempts"] == 0
    attempts = []
    def handler(request):
        attempts.append(request)
        return httpx.Response(500 if len(attempts) == 1 else 200)
    original = httpx.AsyncClient
    monkeypatch.setenv("RESOLVEDESK_ALERT_WEBHOOK", "http://local-test/alerts")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    await monitor.deliver()
    assert monitor.refresh("demo")["notifications"][0]["state"] == "pending"
    with monitor.store.db(True) as db: db.execute("UPDATE alert_outbox SET next_attempt=0")
    await monitor.deliver()
    notification = monitor.refresh("demo")["notifications"][0]
    assert notification["state"] == "delivered" and notification["attempts"] == 2
    assert attempts[0].headers["Idempotency-Key"] == attempts[1].headers["Idempotency-Key"]


def test_latency_alert_and_prometheus(tmp_path):
    monitor = Monitor(Store(tmp_path/"latency.sqlite3"))
    for _ in range(3): monitor.record("demo", "run", "desk", True, 31)
    p = Principal("demo", "support", "support")
    assert monitor.snapshot(p)["active_alerts"][0]["payload"]["rule"] == "latency"
    assert 'resolvedesk_window_p95_ms{kind="run",name="desk"} 31000' in monitor.prometheus(p)
