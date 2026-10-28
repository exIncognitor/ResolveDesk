"""Retirement only touches explicitly authored synthetic evaluations and resources."""
from knowledge_fixtures import publish_fixture
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from resolvedesk.eval_retention import EvalRetention,ServiceResourceCleaner,VerifiedArchive,resource_plan
from resolvedesk.eval_models import EvalDataset
from resolvedesk.eval_sandbox import freeze,EvalSandbox
from resolvedesk.eval_state import EvalState
from resolvedesk.evaluations import EvaluationService
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.store import Principal,Store,Conflict,digest,encode
from test_database_backends import postgres_store
from test_resolvedesk import FakeEmbedding

SUPPORT=Principal('test-eval-tenant','operator','support')


def seed_job(store,job_id='old',*,status='completed',owner=SUPPORT.user):
    with store.db(True) as db:
        db.execute('INSERT INTO eval_jobs(id,tenant,owner,status,completed,total,result,error,created,options) VALUES(?,?,?,?,1,1,?,NULL,1,?)',
            (job_id,SUPPORT.tenant,owner,status,encode({'kind':'retrieval','results':['synthetic evidence']}),encode({'kind':'retrieval','dataset':{'private':'authored inline data'}})))


class DisposableArchive:
    def verify(self,plan):
        assert plan['tenant']==SUPPORT.tenant
        return {'kind':'explicitly disposable synthetic fixture'}


def test_preview_scope_pin_protection_confirmation_and_tombstones(tmp_path):
    store=Store(tmp_path/'synthetic.sqlite3');service=EvaluationService(store,None)
    authored=service.datasets.create(SUPPORT,{'intent_cases':[{'message':'你好','expected':'general'}]})
    seed_job(store);seed_job(store,'pinned');seed_job(store,'active',status='running');seed_job(store,'peer',owner='peer')
    state=EvalState(store,'old');state.snapshot(lambda db:{'cases':['fixed synthetic']})
    state.result('one',{'answer':'stored'})
    with store.db(True) as db:db.execute("INSERT INTO quality_baselines VALUES(?, 'pinned', 'pinned', '{}', 1)",(SUPPORT.tenant,))
    retention=service.retention
    preview=retention.preview(SUPPORT,days=7)
    assert [p['job_id'] for p in preview['plans']]==['old']
    assert preview['protected'][0]['job_id']=='pinned'
    with pytest.raises(PermissionError):retention.preview(Principal(SUPPORT.tenant,'user'))
    with pytest.raises(KeyError):retention.preview(SUPPORT,job_id='peer')
    with pytest.raises(Conflict):retention.apply(SUPPORT,'active','fake',None,DisposableArchive())
    with pytest.raises(Conflict):retention.apply(SUPPORT,'old','stale',None,DisposableArchive())
    result=retention.apply(SUPPORT,'old',preview['plans'][0]['confirmation'],None,DisposableArchive())
    assert result['applied'] and service.get(SUPPORT,'old')['status']=='expired'
    with store.db() as db:
        unit=db.execute("SELECT state,payload FROM eval_units WHERE job_id='old'").fetchone()
        assert unit['state']=='expired' and unit['payload'] is None
        snapshot=json.loads(db.execute("SELECT payload FROM eval_snapshots WHERE job_id='old'").fetchone()[0])
        assert snapshot['retired'] and 'cases' not in snapshot
    assert service.datasets.get(SUPPORT,authored['id'])['payload']['intent_cases']
    with pytest.raises(Conflict):service.resume(SUPPORT,'old')
    with pytest.raises(Conflict):service.set_baseline(SUPPORT,'old')


def test_backup_hash_and_coverage_are_verified_and_legacy_resources_not_guessed(tmp_path):
    path=tmp_path/'synthetic.dump';path.write_bytes(b'explicit synthetic test archive'*10)
    archive=VerifiedArchive(path,hashlib.sha256(path.read_bytes()).hexdigest(),{'test_schema'})
    assert archive.verify({'resources':{'schema':'test_schema'}})['sha256']
    with pytest.raises(ValueError,match='does not cover'):archive.verify({'resources':{'schema':'public'}})
    path.write_bytes(b'modified synthetic archive'*10)
    with pytest.raises(ValueError,match='verification'):archive.verify({'resources':None})
    with pytest.raises(ValueError,match='Legacy'):resource_plan({'identity':'eval_rd_'+uuid.uuid4().hex,'backend':'postgresql'})
    with pytest.raises(ValueError):resource_plan({'identity':'public','backend':'postgresql'})


def test_explicit_retirement_releases_capacity_without_deleting_authored_data(tmp_path):
    store=Store(tmp_path/'capacity.sqlite3');service=EvaluationService(store,None)
    authored=service.datasets.create(SUPPORT,{'intent_cases':[{'message':'你好','expected':'general'}]})
    for index in range(50):seed_job(store,str(index))
    with store.db(True) as db:
        with pytest.raises(Conflict,match='capacity'):service._admission(db,SUPPORT,new=True)
    plan=service.retention.preview(SUPPORT,job_id='0')['plans'][0]
    service.retention.apply(SUPPORT,'0',plan['confirmation'],None,DisposableArchive())
    with store.db(True) as db:service._admission(db,SUPPORT,new=True)
    assert service.datasets.get(SUPPORT,authored['id'])['fingerprint']==authored['fingerprint']


@pytest.mark.services
@pytest.mark.asyncio
async def test_real_services_retire_exact_sandbox_and_recover_after_remote_failure(postgres_store,tmp_path,monkeypatch):
    from resolvedesk.coordination import Coordination
    from resolvedesk.settings import ServiceSettings
    from resolvedesk.vectors import ChromaVectors
    from tools.retain_evaluations import backup_local_compose
    settings=ServiceSettings.load();store=postgres_store
    service=EvaluationService(store,None)
    source_name='test-retention-source-'+uuid.uuid4().hex
    source_vectors=ChromaVectors(settings.chroma_url,FakeEmbedding.identity,collection=source_name)
    coordination=Coordination(settings.redis_url,'test-retention-'+uuid.uuid4().hex)
    kb=HybridKnowledge(store,FakeEmbedding(),source_vectors)
    publish_fixture(kb,SUPPORT,'policy','VPN','VPN 合成制度，原始证据必须保留。')
    identity=uuid.uuid4().hex;seed_job(store,identity)
    state=EvalState(store,identity);state.embedding_identity=FakeEmbedding.identity;state.redis_namespace=coordination.namespace
    manifest=freeze(state,SUPPORT,EvalDataset(intent_cases=[{'message':'你好','expected':'general'}]).model_dump(),Path(__file__).resolve().parents[1])
    sandbox=EvalSandbox(store,kb,manifest,tmp_path/'unused',coordination)
    cleaner=ServiceResourceCleaner(settings.redis_url,settings.chroma_url)
    try:
        await sandbox.open(SUPPORT)
        namespace=manifest['resources']['redis_namespace']
        cleaner.redis.set(namespace+':synthetic-key','value')
        cleaner.redis.set(coordination.namespace+':source-kept','value')
        await sandbox.close()
        archive=backup_local_compose(store)
        plan=service.retention.preview(SUPPORT,job_id=identity)['plans'][0]
        # An outside dependency must block retirement without deleting the view
        # or any remote vectors. DROP ... CASCADE is deliberately forbidden.
        with store.db(True) as db:
            db.execute('CREATE VIEW external_synthetic_dependency AS SELECT id FROM "'+manifest['identity']+'".runs')
        original_delete=cleaner.http.delete
        deletes=[]
        def track_delete(*args,**kwargs):deletes.append(args[0]);return original_delete(*args,**kwargs)
        monkeypatch.setattr(cleaner.http,'delete',track_delete)
        with pytest.raises(DBAPIError) as held:service.retention.apply(SUPPORT,identity,plan['confirmation'],cleaner,archive)
        assert held.value.orig.sqlstate=='2BP01'
        assert not deletes
        with store.db(True) as db:
            assert db.execute('SELECT COUNT(*) FROM external_synthetic_dependency').fetchone()[0]==0
            db.execute('DROP VIEW external_synthetic_dependency')
        calls=[]
        def fail_once(*args,**kwargs):
            calls.append(args[0])
            if len(calls)==1:raise TimeoutError('synthetic pre-delete transport failure')
            return original_delete(*args,**kwargs)
        monkeypatch.setattr(cleaner.http,'delete',fail_once)
        with pytest.raises(TimeoutError):service.retention.apply(SUPPORT,identity,plan['confirmation'],cleaner,archive)
        assert service.get(SUPPORT,identity)['status']=='retire_failed'
        assert cleaner.redis.get(namespace+':synthetic-key')=='value'
        again=service.retention.preview(SUPPORT,job_id=identity)['plans'][0]
        assert again['confirmation']==plan['confirmation']
        result=service.retention.apply(SUPPORT,identity,again['confirmation'],cleaner,archive)
        assert result['status']=='expired'
        assert cleaner.redis.get(namespace+':synthetic-key') is None
        assert cleaner.redis.get(coordination.namespace+':source-kept')=='value'
        assert source_vectors.count()==1
        for name in (manifest['resources']['knowledge_collection'],manifest['resources']['memory_collection']):assert cleaner.http.get(cleaner.base+'/'+name).status_code==404
        with store.db() as db:
            assert not db.execute('SELECT 1 FROM pg_namespace WHERE nspname=?',(manifest['identity'],)).fetchone()
            assert db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]==1
        assert archive.path.is_file()  # Recovery evidence is deliberately retained.
    finally:
        await sandbox.close()
        # Failure evidence is retained if retirement did not complete; never guess
        # cleanup targets or remove another fixture's resources.
        result=source_vectors.http.delete(source_vectors.base+'/'+source_name)
        assert result.status_code in {200,404}
        cleaner.redis.delete(coordination.namespace+':source-kept')
        cleaner.close();source_vectors.close();await coordination.close()
