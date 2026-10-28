"""A migration preserves managed drafts, not just legacy row counts."""
import pytest
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.store import Store
from test_database_backends import postgres_store
from test_resolvedesk import SUPPORT,USER
from tools.migrate_services import initialize,copy_to_postgres,snapshot


@pytest.mark.services
def test_copy_managed_source_preserves_unreviewed_status_and_marker(tmp_path,postgres_store):
    source=Store(tmp_path/'managed.sqlite3');kb=HybridKnowledge(source)
    kb.add(SUPPORT,'new-draft','VPN draft','VPN unreviewed material')
    with source.db() as db:
        marker=dict(db.execute('SELECT * FROM knowledge_governance_migrations').fetchone())
    backup=snapshot(source.path,tmp_path/'managed-snapshot.sqlite3',application=True)
    initialize(postgres_store);copy_to_postgres(backup,postgres_store)
    migrated=HybridKnowledge(postgres_store)
    assert not migrated.search(USER,'VPN')['items']
    assert migrated.publications(SUPPORT)[0]['state']=='draft'
    with postgres_store.db() as db:
        assert dict(db.execute('SELECT * FROM knowledge_governance_migrations').fetchone())==marker
        assert not db.execute('SELECT * FROM knowledge_legacy_documents').fetchall()
    source.close()
