from concurrent.futures import ThreadPoolExecutor
import os
import uuid

import pytest
from sqlalchemy import text

from resolvedesk.database import bindings
from resolvedesk.store import Conflict, Principal, Store
from resolvedesk.memory import MEMORY_TABLES
from resolvedesk.metrics import METRIC_TABLES


def test_bound_parameters_preserve_literals_comments_and_values():
    sql, params = bindings("SELECT '?' AS literal, ? AS actual -- ?\n/* ? */ WHERE '?'='?' AND x=?", ("value with 'quotes'", 2))
    assert sql.count(":p0") == 1 and sql.count(":p1") == 1
    assert "-- ?" in sql and "/* ? */" in sql and "'?'" in sql
    assert params == {"p0":"value with 'quotes'","p1":2}
    with pytest.raises(ValueError): bindings("SELECT ?", ())
    with pytest.raises(ValueError): bindings("SELECT 1", (2,))


@pytest.fixture
def postgres_store():
    if os.getenv("RESOLVEDESK_SERVICE_TESTS") != "true": pytest.skip("Real dependency tests explicitly disabled")
    from resolvedesk.settings import ServiceSettings
    schema = "test_rd_"+uuid.uuid4().hex
    store = Store(ServiceSettings.load().database_url,schema=schema)
    allowed = MEMORY_TABLES|{"runs","events","messages","tickets","feedback","checkpoints","turn_receipts","entity_state","desk_assets","service_cases","service_audit","service_actions","desk_grants",
               "chunks","knowledge_documents","index_outbox","memory_summary","memory_episode","memory_profile","eval_jobs","regression_cases","quality_baselines","telemetry","desk_alerts","alert_outbox",
               'eval_suites','eval_snapshots','eval_units','eval_queue_guard','eval_retirements'}|METRIC_TABLES
    try: yield store
    finally:
        # This unique schema was created by this fixture in this task. Never
        # touch public or a schema with unexpected user/other-task tables.
        with store.database.engine.begin() as db:
            tables = set(db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname=:schema"),{"schema":schema}).scalars())
            assert schema.startswith("test_rd_") and tables <= allowed
            db.exec_driver_sql('DROP SCHEMA "'+schema+'" CASCADE')
        store.close()


@pytest.mark.services
def test_postgres_real_business_and_all_current_repositories(postgres_store):
    from resolvedesk.business import BusinessService
    from resolvedesk.retrieval import HybridKnowledge
    from resolvedesk.services import ScopedMemory
    from resolvedesk.evaluations import EvaluationService
    from resolvedesk.monitoring import Monitor
    from test_it_workflows import ACCESS,REPAIR,USER,SUPPORT,submit,action
    store = postgres_store
    business = BusinessService(store);business.seed_demo()
    kb = HybridKnowledge(store)
    kb.add(SUPPORT,"test-policy","测试制度","# 测试制度\n\n权限申请最长30天。")
    assert kb.search(USER,"权限",mode="bm25")["items"]
    memory = ScopedMemory(store,None,None)
    evaluation = EvaluationService(store,kb)
    monitor = Monitor(store)
    access = submit(store,ACCESS,key="postgres-access")
    case = business.get(USER,access["id"])
    case = action(business,SUPPORT,case,"approve")
    case = action(business,SUPPORT,case,"provision")
    assert business.context(USER)["my_grants"][0]["level"] == "read"
    repair = submit(store,REPAIR,key="postgres-repair")
    case = business.get(USER,repair["id"])
    for verb in ("triage","assign","start","resolve","accept"):
        case = action(business,USER if verb=="accept" else SUPPORT,case,verb,**({"assignee":"support"} if verb=="assign" else {}))
    assert case["status"] == "closed"
    monitor.record("demo","run","desk",True,.25)
    assert monitor.snapshot(SUPPORT)["run_stats"]["desk"]["p95_ms"] == 250
    assert evaluation.list(SUPPORT) == [] and memory.inspect(USER)["profile"] == {}


@pytest.mark.services
def test_postgres_competing_create_confirm_and_asset_claim(postgres_store):
    from resolvedesk.business import BusinessService
    from test_it_workflows import REPAIR,USER,submit
    store = postgres_store
    BusinessService(store).seed_demo()
    with ThreadPoolExecutor(max_workers=8) as pool:
        created = list(pool.map(lambda _:store.create_run(USER,"相同请求","postgres-same-key"),range(8)))
    assert sum(new for _,new in created)==1 and len({r["id"] for r,_ in created})==1
    def repair(index):
        try:return submit(store,REPAIR,key="concurrent-repair-"+str(index))["id"]
        except Conflict:return "conflict"
    with ThreadPoolExecutor(max_workers=4) as pool: results = list(pool.map(repair,range(4)))
    assert results.count("conflict")==3
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM service_cases").fetchone()[0]==1


@pytest.mark.services
def test_postgres_concurrent_schema_initialization_and_time_precision(postgres_store):
    from resolvedesk.services import ScopedMemory
    store = postgres_store
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _:ScopedMemory(store,None,None),range(4)))
    precise = 1793076000.123456
    with store.db(True) as db:
        db.execute("INSERT INTO memory_episode VALUES(?,?,?,?,?,?,?)",("precision","demo","u","test","[]","test",precise))
    with store.db() as db:
        assert abs(db.execute("SELECT updated FROM memory_episode").fetchone()[0]-precise)<.00001
    from resolvedesk.evaluations import EvaluationService
    EvaluationService(store,None)
    with store.db(True) as db:
        db.execute("INSERT INTO quality_baselines VALUES('test','precision','test','{}',?)",(precise,))
    with store.db() as db:
        assert abs(db.execute("SELECT created FROM quality_baselines").fetchone()[0]-precise)<.00001


@pytest.mark.services
def test_postgres_fencing_uses_database_clock_despite_worker_clock_skew(postgres_store,monkeypatch):
    import resolvedesk.store as module
    from types import SimpleNamespace
    p=Principal("demo","clock-worker")
    run,_=postgres_store.create_run(p,"clock test","clock-key")
    monkeypatch.setattr(module,"time",SimpleNamespace(time=lambda:0))
    token=postgres_store.claim_execution(p,run["id"],"worker",60)
    assert postgres_store.recover_expired()==0
    assert postgres_store.renew_execution(p,token)
    monkeypatch.setattr(module,"time",SimpleNamespace(time=lambda:9999999999))
    with postgres_store.execution(token):postgres_store.checkpoint(p,run["id"],{"valid":True})
    assert postgres_store.recover_expired()==0
