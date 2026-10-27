from pathlib import Path
import subprocess
import sys

import pytest

from test_database_backends import postgres_store
from test_redis_coordination import cache
from test_resolvedesk import USER


@pytest.mark.services
def test_separate_python_processes_share_sql_fencing_and_redis_lease(postgres_store,cache):
    store=postgres_store
    run,_=store.create_run(USER,"synthetic concurrency","process-test")
    root=Path(__file__).resolve().parents[1]
    command=[sys.executable,"-X","utf8","-B",str(root/"tests/service_worker_fixture.py"),store.database.schema,cache.namespace,run["id"]]
    workers=[]
    try:
        for _ in range(2): workers.append(subprocess.Popen(command,cwd=root,stdout=subprocess.PIPE,stderr=subprocess.PIPE))
        for worker in workers:
            output,error=worker.communicate(timeout=20)
            assert worker.returncode==0,(output+error).decode(errors="replace")[-2000:]
        assert store.get_run(USER,run["id"])["answer"]=="offline process result"
        assert sum(e["kind"]=="offline_fixture_model_call" for e in store.events(USER,run["id"]))==1
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.terminate();worker.wait(timeout=10)
