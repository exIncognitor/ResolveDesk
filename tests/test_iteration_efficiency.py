"""Efficiency must preserve scope and leave low-confidence/write paths intact."""
from knowledge_fixtures import publish_fixture
from concurrent.futures import ThreadPoolExecutor
import math

import pytest

from resolvedesk.retrieval import BM25Index,HybridKnowledge
from resolvedesk.store import Store
from test_resolvedesk import USER,OTHER,SUPPORT,FOREIGN
from test_reliable_retrieval import environment


def test_postings_keep_the_public_bm25_formula_and_multilingual_order():
    # Equal-length documents make the expected BM25 values hand-computable:
    # N=3, df(vpn)=2, length/average=1, tf=1 or 2.
    index=BM25Index(['VPN code','VPN VPN','DNS code'])
    scores=index.scores('VPN')
    assert scores==pytest.approx([math.log(1.6),math.log(1.6)*4.4/3.2,0])
    assert BM25Index([]).scores('empty')==[]
    chinese=BM25Index(['账号锁定官方入口','网络连不上先排查','许可证到期回收'])
    assert max(range(3),key=lambda i:chinese.scores('账号锁定')[i])==0


def test_lexical_snapshot_refreshes_for_same_content_acl_change_and_cross_worker(tmp_path):
    store=Store(tmp_path/'postings.sqlite3');one=HybridKnowledge(store);two=HybridKnowledge(store)
    try:
        one.add(SUPPORT,'scope','VPN','VPN 连接超时')
        before=two.scope_version(USER)
        assert two.search(USER,'VPN',mode='bm25')['items']
        one.add(SUPPORT,'scope','VPN','VPN 连接超时',visibility='private')
        assert two.scope_version(USER)!=before and two.search(USER,'VPN',mode='bm25')['items']==[]
        assert two.search(SUPPORT,'VPN',mode='bm25')['items']
        one.add(SUPPORT,'scope','新版','VPN新版诊断')
        current=two.search(USER,'VPN',mode='bm25')['items']
        assert current[0]['content']=='VPN新版诊断' and two.search(FOREIGN,'VPN',mode='bm25')['items']==[]
    finally:store.close()


def test_concurrent_queries_share_only_current_authorized_content(tmp_path):
    store=Store(tmp_path/'parallel.sqlite3');kb=HybridKnowledge(store)
    try:
        publish_fixture(kb,SUPPORT,'shared','共享制度','VPN网络诊断')
        publish_fixture(kb,SUPPORT,'private','支持人员私有制度','VPN_SECRET_MARKER',visibility='private')
        def query(index):return kb.search(USER if index%2 else OTHER,'VPN',mode='bm25')['items']
        with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(query,range(40)))
        assert all(rows and all(row['document_id']=='shared' and 'SECRET' not in row['content'] for row in rows) for rows in results)
    finally:store.close()


@pytest.mark.asyncio
async def test_adaptive_strong_original_evidence_avoids_paid_rewrite_and_rerank(environment):
    store,kb,model,services=environment
    services.retrieval.optimization_policy='adaptive'
    try:
        result=await services.search(USER,'VPN',2)
        assert result['items'] and result['optimization_plan']=='sufficient_original_evidence'
        assert len(result['queries'])==1 and not result['reranked']
        model.messages.create.assert_not_called()
    finally:await services.close()


@pytest.mark.asyncio
async def test_adaptive_infra_outage_does_not_pay_for_rewrites(environment,monkeypatch):
    store,kb,model,services=environment;original=kb.search
    services.retrieval.optimization_policy='adaptive'
    def broken(p,q,k,mode):
        if mode!='bm25':raise ConnectionError('Synthetic Chroma outage')
        return original(p,q,k,mode)
    monkeypatch.setattr(kb,'search',broken)
    try:
        result=await services.search(USER,'VPN故障',2)
        assert result['items'] and result['degraded'] and result['optimization_plan']=='dependency_degraded'
        model.messages.create.assert_not_called()
    finally:await services.close()


@pytest.mark.asyncio
async def test_direct_master_policy_never_skips_composite_low_confidence_or_write_requests(environment):
    store,kb,model,services=environment
    services.execution_policy='adaptive'
    source=kb.search(USER,'VPN',1)['items'][0]
    run,_=store.create_run(USER,'VPN报错','simple-efficiency')
    store.claim(USER,run['id'])
    context={'evidence':[source],'retrieval_degraded':False}
    route={'confidence':.9,'fine_intent':'vpn_failure','domains':['network_endpoint'],'supporting':[],'urgency':'LOW','primary':'technical','intent':'network_endpoint'}
    try:
        direct=await services.consult(USER,run['id'],'VPN809怎么排查',[],context,route)
        assert direct['execution_path']=='direct_master' and direct['actual_roles']==[] and not direct['specialist_opinions']
        # These must go through actual expert execution; a deliberate fixture
        # failure establishes that the direct branch was not taken.
        async def disabled(*a,**kw):raise RuntimeError('Synthetic expert boundary')
        services.roles._execute=disabled
        cases=[('请准备申请',route),('VPN和授权',route|{'domains':['network_endpoint','software_license']}),
            ('VPN怎么排查',route|{'confidence':.4}),('VPN怎么排查',route|{'urgency':'CRITICAL'})]
        for message,routing in cases:
            result=await services.consult(USER,run['id'],message,[],context,routing)
            assert result['status']=='failed' and result.get('execution_path')!='direct_master'
    finally:await services.close()
