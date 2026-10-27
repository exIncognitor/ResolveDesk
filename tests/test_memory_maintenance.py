"""Memory behavior through current foreground, workers and real dependencies."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid
import threading

import pytest

from resolvedesk.memory import ScopedMemory, MemoryLeaseLost, safe_text
from resolvedesk.store import Principal, Store, encode
from test_resolvedesk import USER, OTHER, FOREIGN, FakeEmbedding, response, client_with
from test_database_backends import postgres_store
from test_chroma_projection import vectors
from test_redis_coordination import cache


@pytest.fixture
def store(tmp_path):
    result=Store(tmp_path/'memory.sqlite3')
    try:yield result
    finally:result.close()


def conversation(store,p=USER,turns=1,message='我使用 Windows 11 和 IntelliJ，VPN 连接失败',key=None):
    run,_=store.create_run(p,message,key or 'memory-'+uuid.uuid4().hex)
    for index in range(turns):
        store.claim(p,run['id'])
        store.finish(p,run['id'],'completed','VPN 排查结果 '+str(index))
        if index+1<turns:
            store.reply(p,run['id'],'VPN 仍然失败，已尝试步骤 '+str(index))
    return run['id']


async def drain(memory,limit=50):
    for _ in range(limit):
        if not await memory.process_one():return
    raise AssertionError('Memory queue did not converge')


@pytest.mark.asyncio
async def test_foreground_never_waits_for_model_and_segment_recall_is_private(store):
    model=client_with(response('{"summary":"VPN 故障未解决","facts":[]}'))
    memory=ScopedMemory(store,FakeEmbedding(),model)
    old=conversation(store,turns=8)
    before=store.history(USER,old)
    context=await memory.context(USER,old)
    assert context['summary_pending'] and context['working_messages']
    model.messages.create.assert_not_called()
    await drain(memory)
    assert memory.inspect(USER)['episodes']==4
    assert memory.inspect(USER)['profile']=={'os':'Windows 11','ide':'IntelliJ'}
    assert memory.inspect(USER)['maintenance']['state']=='idle'
    current=conversation(store,message='VPN 同样报错')
    await memory.remember(USER,current);await drain(memory)
    context=await memory.context(USER,current)
    assert context['relevant_history'][0]['same_run']
    assert any(r['run_id']==old and not r['same_run'] for r in context['relevant_history'])
    assert store.history(USER,old)==before
    assert memory.inspect(OTHER)['profile']=={} and memory.inspect(FOREIGN)['episodes']==0
    with pytest.raises(KeyError):await memory.context(OTHER,current)


@pytest.mark.asyncio
async def test_profile_requires_exact_user_evidence_and_newer_cross_run_fact_wins(store):
    message='我使用 Windows 11，偏好先给命令再解释，产品是企业VPN，问题是连接超时'
    first=conversation(store,message=message)
    seq=store.history(USER,first)[0]['seq'] if 'seq' in store.history(USER,first)[0] else None
    with store.db() as db:seq=db.execute("SELECT MIN(seq) FROM messages WHERE run_id=?",(first,)).fetchone()[0]
    facts=[{'category':'preference','field':'preference','value':'先给命令再解释','source_seq':seq,'quote':'偏好先给命令再解释'},
           {'category':'product','field':'product','value':'企业VPN','source_seq':seq,'quote':'产品是企业VPN'},
           {'category':'problem_type','field':'problem_type','value':'连接超时','source_seq':seq,'quote':'问题是连接超时'},
           {'category':'environment','field':'os','value':'Ubuntu 24','source_seq':seq,'quote':'Ubuntu 24'},
           {'category':'preference','field':'preference','value':'虚构偏好','source_seq':seq+1,'quote':'虚构偏好'}]
    memory=ScopedMemory(store,FakeEmbedding(),client_with(response(encode({'facts':facts}))))
    await memory.remember(USER,first);await drain(memory)
    profile=memory.inspect(USER)['profile']
    assert profile['os']=='Windows 11' and profile['preferences']==['先给命令再解释']
    assert profile['entities']=={'products':['企业VPN'],'problem_types':['连接超时']}
    second=conversation(store,message='现在我使用 Ubuntu 24，VPN 依旧失败')
    await memory.remember(USER,second);await drain(memory)
    assert memory.inspect(USER)['profile']['os']=='Ubuntu 24'
    with store.db(True) as db:
        memory._merge_facts(db,{'tenant':USER.tenant,'owner':USER.user},first,
            [{'category':'environment','field':'os','value':'Windows 11','source_seq':seq,'quote':'Windows 11'}])
    assert memory.inspect(USER)['profile']['os']=='Ubuntu 24'


@pytest.mark.asyncio
async def test_summary_merges_only_new_older_messages_and_failures_do_not_retry_paid_cursor(store):
    model=client_with(response('{"summary":"保留首次 VPN 错误","facts":[]}'))
    memory=ScopedMemory(store,FakeEmbedding(),model)
    run=conversation(store,turns=5)
    await memory.remember(USER,run);await drain(memory)
    with store.db() as db:previous=dict(db.execute('SELECT * FROM memory_summary WHERE run_id=?',(run,)).fetchone())
    store.reply(USER,run,'新 VPN 信息');store.claim(USER,run);store.finish(USER,run,'completed','新建议')
    model.messages.create.side_effect=TimeoutError('synthetic')
    await memory.remember(USER,run);await drain(memory)
    assert model.messages.create.call_count==2
    payload=json.loads(model.messages.create.call_args.kwargs['messages'][0]['content'])
    assert payload['previous_summary']==previous['content']
    with store.db() as db:
        state=dict(db.execute('SELECT * FROM memory_runs WHERE run_id=?',(run,)).fetchone())
        summary=dict(db.execute('SELECT * FROM memory_summary WHERE run_id=?',(run,)).fetchone())
    assert summary['upto']>previous['upto'] and previous['content'] in summary['content']
    assert state['status']=='degraded' and 'TimeoutError' in state['error']
    await memory.context(USER,run);await drain(memory)
    assert model.messages.create.call_count==2
    assert store.get_run(USER,run)['status']=='completed'


@pytest.mark.asyncio
async def test_queue_coalesces_user_caps_and_backlog_recovers_without_message_loss(store):
    memory=ScopedMemory(store,FakeEmbedding(),None,tenant_capacity=1,global_capacity=2)
    first=conversation(store);second=conversation(store);other=conversation(store,OTHER)
    assert await memory.remember(USER,first) and await memory.remember(USER,second)
    assert not await memory.remember(OTHER,other)
    with store.db() as db:assert db.execute("SELECT COUNT(*) FROM memory_users WHERE state<>'idle'").fetchone()[0]==1
    await drain(memory)
    memory.recover_backlog();await drain(memory)
    assert memory.inspect(USER)['episodes']==2 and memory.inspect(OTHER)['episodes']==1
    assert len(store.history(OTHER,other))==2


@pytest.mark.asyncio
async def test_crashed_paid_attempt_falls_back_and_assistant_tail_is_not_skipped(store):
    model=client_with()
    memory=ScopedMemory(store,FakeEmbedding(),model)
    run,_=store.create_run(USER,'我使用 Windows 11，VPN 失败','crash-memory')
    await memory.context(USER,run['id'])
    job=memory._claim();batch=memory._batch(job)
    assert batch['paid']
    with store.db(True) as db:db.execute("UPDATE memory_users SET lease_until=0")
    await drain(memory)
    model.messages.create.assert_not_called()
    store.claim(USER,run['id']);store.finish(USER,run['id'],'completed','VPN 尾部结果')
    await memory.remember(USER,run['id']);await drain(memory)
    with store.db() as db:
        segment=db.execute('SELECT e.* FROM memory_segments e JOIN memory_slots c ON c.segment_id=e.id WHERE e.run_id=?',(run['id'],)).fetchone()
        assert segment['last_seq']==db.execute('SELECT MAX(seq) FROM messages WHERE run_id=?',(run['id'],)).fetchone()[0]
        assert 'VPN 尾部结果' in segment['content']
        assert db.execute('SELECT COUNT(*) FROM memory_slots WHERE run_id=?',(run['id'],)).fetchone()[0]==1
    assert len(store.history(USER,run['id']))==2


def test_redaction_precedes_memory_slicing_and_chinese_explicit_environment(store,monkeypatch):
    secret='synthetic-credential-value-for-memory-test'
    monkeypatch.setenv('ANTHROPIC_API_KEY',secret)
    text='x'*995+' '+secret+' password=other-synthetic-value'
    clean=safe_text(text,1600)
    assert secret not in clean and 'other-synthetic-value' not in clean and '[REDACTED]' in clean
    memory=ScopedMemory(store,None,None)
    facts=memory._explicit_facts([{'seq':1,'role':'user','content':'我使用Windows 11和IntelliJ IDEA，不用PyCharm'}])
    assert {f['field']:f['value'] for f in facts}=={'os':'Windows 11','ide':'IntelliJ IDEA'}


@pytest.mark.asyncio
async def test_timed_out_embedding_threads_keep_slots_until_they_actually_finish(store):
    release=threading.Event();started=[]
    class HungEmbedding:
        identity='test-bounded-hang'
        def embed(self,texts,query=False):
            started.append(texts);release.wait(2)
            return [[1.,0.]]
    memory=ScopedMemory(store,HungEmbedding(),None,workers=2)
    try:
        results=await asyncio.gather(*(memory._embed(['VPN'],timeout=.05) for _ in range(8)),return_exceptions=True)
        assert all(isinstance(r,TimeoutError) for r in results)
        assert len(started)==2
    finally:
        release.set()
        async with asyncio.timeout(3):
            while memory.embedding_slots._value!=2:await asyncio.sleep(.01)


@pytest.mark.asyncio
async def test_stale_maintenance_commit_is_fenced_and_restart_uses_same_queue(store):
    memory=ScopedMemory(store,FakeEmbedding(),None)
    run=conversation(store)
    await memory.remember(USER,run)
    old=memory._claim();batch=memory._batch(old)
    with store.db(True) as db:db.execute('UPDATE memory_users SET lease_until=0')
    successor=ScopedMemory(store,FakeEmbedding(),None)
    claim=successor._claim()
    with pytest.raises(MemoryLeaseLost):await memory._maintain(old,batch)
    successor._release(claim,'test release')
    with store.db(True) as db:db.execute('UPDATE memory_users SET next_attempt=0')
    await drain(successor)
    assert successor.inspect(USER)['episodes']==1


@pytest.mark.services
@pytest.mark.asyncio
async def test_real_private_chroma_redis_cache_outage_recovery_and_current_slot_validation(postgres_store,vectors,cache,monkeypatch):
    memory=ScopedMemory(postgres_store,FakeEmbedding(),None,vector_index=vectors,coordination=cache)
    try:
        first=conversation(postgres_store,turns=6)
        other=conversation(postgres_store,OTHER,turns=2)
        foreign=conversation(postgres_store,FOREIGN,turns=2)
        for p,run in ((USER,first),(OTHER,other),(FOREIGN,foreign)):
            await memory.remember(p,run)
        await drain(memory)
        assert memory.drain_index(10)=={'done':3,'failed':0}
        current=conversation(postgres_store,message='VPN 再次失败')
        context=await memory.context(USER,current)
        assert context['backend']=='chroma-private' and not context['degraded']
        assert {r['run_id'] for r in context['relevant_history']}=={first}
        hit=await memory.context(USER,current)
        assert hit['cache_hit']
        with postgres_store.db(True) as db:
            source=db.execute('SELECT e.* FROM memory_segments e JOIN memory_slots c ON c.segment_id=e.id WHERE e.run_id=? LIMIT 1',(first,)).fetchone()
            stale=dict(source)
            db.execute('DELETE FROM memory_slots WHERE segment_id=?',(stale['id'],))
        candidates=[{'id':stale['id'],'cosine':1.,'metadata':{'tenant':USER.tenant,'owner':USER.user,'visibility':'private','version':stale['id']}}]
        assert memory._validate_matches(USER,current,[],candidates)==[]
        # New maintenance invalidates Redis by revision, and an index outage
        # retains SQL + durable retry instead of falling back to a SQL scan.
        original=vectors.replace
        monkeypatch.setattr(vectors,'replace',lambda *args:(_ for _ in ()).throw(ConnectionError('synthetic')))
        await memory.remember(USER,current);await drain(memory)
        assert memory.drain_index(10)['failed']>=1
        monkeypatch.setattr(vectors,'replace',original)
        with postgres_store.db(True) as db:db.execute('UPDATE memory_outbox SET next_attempt=0')
        assert memory.drain_index(10)['done']>=1
        assert not (await memory.context(USER,current))['cache_hit']
        monkeypatch.setattr(vectors,'search',lambda *args,**kwargs:(_ for _ in ()).throw(ConnectionError('synthetic')))
        newer=conversation(postgres_store,message='VPN 新查询')
        degraded=await memory.context(USER,newer)
        assert degraded['degraded'] and degraded['relevant_history']==[] and degraded['backend']=='chroma-private'
    finally:
        keys=[key async for key in cache.redis.scan_iter(match=cache.namespace+':*')]
        assert all(k.startswith(cache.namespace+':') for k in keys)
        if keys:await cache.redis.delete(*keys)
        await cache.close()


@pytest.mark.services
def test_real_postgres_only_one_user_claim_across_competing_instances(postgres_store):
    memory=ScopedMemory(postgres_store,FakeEmbedding(),None)
    run=conversation(postgres_store)
    memory.enqueue(USER,run)
    other=ScopedMemory(postgres_store,FakeEmbedding(),None)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims=list(pool.map(lambda i:(memory if i%2 else other)._claim(),range(8)))
    assert sum(job is not None for job in claims)==1
