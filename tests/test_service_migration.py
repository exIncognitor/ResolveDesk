import sqlite3

import pytest

from resolvedesk.store import Store
from test_resolvedesk import USER
from test_database_backends import postgres_store
from tools.migrate_services import snapshot,initialize,copy_to_postgres,sqlite_manifest


def test_consistent_sqlite_snapshot_preserves_committed_content(tmp_path):
    store=Store(tmp_path/"source.sqlite3")
    store.create_run(USER,"original","snapshot-key")
    backup=snapshot(store.path,tmp_path/"snapshot.sqlite3",application=True)
    store.create_run(USER,"later","later-key")
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]==1
    with pytest.raises(ValueError): snapshot(store.path,backup,application=True)
    store.close()


@pytest.mark.services
def test_real_postgres_migration_preserves_hashes_and_refuses_nonempty_target(tmp_path,postgres_store):
    source=Store(tmp_path/"legacy.sqlite3")
    first,_=source.create_run(USER,"old question","legacy-key")
    source.claim(USER,first["id"]);source.finish(USER,first["id"],"completed","old answer")
    # Emulate a source written before durable per-turn receipts were introduced.
    with source.db(True) as db: db.execute("DELETE FROM turn_receipts")
    backup=snapshot(source.path,tmp_path/"snapshot.sqlite3",application=True)
    initialize(postgres_store)
    copied=copy_to_postgres(backup,postgres_store)
    with sqlite3.connect(backup) as db: expected=sqlite_manifest(db)
    assert all(copied[table]["sha256"]==details["sha256"] for table,details in expected.items())
    replay=postgres_store.get_turn(USER,first["id"],"legacy-key")
    assert replay["answer"]=="old answer" and replay["legacy_cutover"]
    newer,_=postgres_store.create_run(USER,"new question","new-key")
    assert postgres_store.latest_user_seq(USER,newer["id"])>source.latest_user_seq(USER,first["id"])
    with pytest.raises(ValueError,match="not empty"): copy_to_postgres(backup,postgres_store)
    assert postgres_store.get_run(USER,newer["id"])["message"]=="new question"
    source.close()
