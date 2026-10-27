"""Real PostgreSQL finance tests use a unique schema and synthetic source only."""
from concurrent.futures import ThreadPoolExecutor
import os
import json
import uuid

import pytest
from sqlalchemy import text

from core.paid_api import BudgetPolicy,CostLedger,PaidCallBlocked
from core.financial_ledger import PostgresCostLedger,manifest
from resolvedesk.settings import ServiceSettings


@pytest.fixture
def finance(tmp_path):
    if os.getenv('RESOLVEDESK_SERVICE_TESTS')!='true':pytest.skip('Real PostgreSQL test explicitly disabled')
    name='test_rd_finance_'+uuid.uuid4().hex
    ledger=PostgresCostLedger(ServiceSettings.load().database_url,BudgetPolicy(),schema=name)
    source=CostLedger(tmp_path/'synthetic.sqlite3',BudgetPolicy())
    try:yield ledger,source
    finally:
        with ledger.database.engine.begin() as db:
            tables=set(db.execute(text('SELECT tablename FROM pg_tables WHERE schemaname=:s'),{'s':name}).scalars())
            assert name.startswith('test_rd_finance_') and tables<={'paid_calls','finance_authority'}
            db.exec_driver_sql('DROP SCHEMA "'+name+'" CASCADE')
        ledger.close()


@pytest.mark.services
def test_exact_import_preserves_paid_and_uncertain_amounts_and_refuses_reset(finance):
    ledger,source=finance
    with pytest.raises(PaidCallBlocked):ledger.reserve(1,1)
    settled=source.reserve(1000,2000);source.settle(settled,100,200)
    uncertain=source.reserve(20,30);source.uncertain(uncertain)
    source.reserve(5,5)
    expected=source.summary()
    with source._transaction() as db:
        db.row_factory=__import__('sqlite3').Row
        rows=[dict(row) for row in db.execute('SELECT * FROM paid_calls')]
    imported=ledger.import_snapshot(source.path)
    assert {k:imported[k] for k in ('count','sha256')}==manifest(rows)
    assert ledger.summary()==expected
    assert json.loads(json.dumps(ledger.summary()))==expected
    with pytest.raises(PaidCallBlocked):ledger.import_snapshot(source.path)
    assert ledger.summary()==expected


@pytest.mark.services
def test_competing_process_equivalent_connections_cannot_overspend(finance):
    ledger,source=finance
    ledger.import_snapshot(source.path)
    def attempt(_):
        try:
            call=ledger.reserve(500000,0)
            ledger.settle(call,500000,0)
            return 1
        except PaidCallBlocked:return 0
    with ThreadPoolExecutor(max_workers=8) as pool:accepted=sum(pool.map(attempt,range(32)))
    assert accepted==18
    assert ledger.summary()['calls']=={'settled':18} and ledger.summary()['available_cny']=='0'
