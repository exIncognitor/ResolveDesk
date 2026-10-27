"""Kill one owned fake-model worker and resume in an independent Python process."""
import asyncio
import os
from pathlib import Path
import sys
import uuid

import pytest

from resolvedesk.evaluations import EvaluationService
from resolvedesk.store import Principal
from test_database_backends import postgres_store


SCRIPT=r'''
import asyncio,sys
from resolvedesk.settings import ServiceSettings
from resolvedesk.store import Store,Principal
from resolvedesk.evaluations import EvaluationService
from resolvedesk.eval_state import UncertainUnit
from resolvedesk.monitoring import Monitor

async def main():
    schema,job_id,phase,mode=sys.argv[1:]
    store=Store(ServiceSettings.load().database_url,schema=schema)
    service=EvaluationService(store,None)
    monitor=Monitor(store)
    principal=Principal('crash-test','operator','support')
    async def work(p,state,progress):
        async def fake_model():
            monitor.record(p.tenant,'synthetic_paid','fake-model',True)
            if phase=='uncertain' and mode=='first':
                print('checkpoint-ready',flush=True)
                await asyncio.Event().wait()
            return {'answer':'committed fake response'}
        try:
            result=await state.unit('synthetic-paid-unit',fake_model)
        except UncertainUnit:
            return {'status':'partial','uncertain_unit':True,'evaluation_errors':1}
        if mode=='first':
            print('checkpoint-ready',flush=True)
            await asyncio.Event().wait()
        return {'status':'complete','evaluation_errors':0,'observed':result}
    service._retrieval=work
    try:
        if mode=='first':await service.run(principal,job_id)
        else:
            service.resume(principal,job_id)
            await asyncio.gather(*list(service.tasks.values()))
    finally:
        await service.close();store.close()

asyncio.run(main())
'''


@pytest.mark.services
@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['completed','uncertain'])
async def test_crash_resume_in_another_process_never_repeats_fake_paid_unit(postgres_store,phase):
    store=postgres_store;service=EvaluationService(store,None)
    job_id=uuid.uuid4().hex
    with store.db(True) as db:
        db.execute("INSERT INTO eval_jobs(id,tenant,owner,status,completed,total,result,error,created,options,queue_since) VALUES(?,'crash-test','operator','queued',0,0,NULL,NULL,?,'{}',?)",(job_id,db.now(),db.now()))
    cwd=Path(__file__).resolve().parents[1]
    async def start(mode):
        env=dict(os.environ);env['RESOLVEDESK_PAID_API_ENABLED']='false'
        return await asyncio.create_subprocess_exec(sys.executable,'-X','utf8','-B','-c',SCRIPT,store.database.schema,job_id,phase,mode,
            cwd=cwd,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    first=await start('first')
    try:
        signal=await asyncio.wait_for(first.stdout.readline(),20)
        assert signal.strip()==b'checkpoint-ready'
        assert first.returncode is None
        first.kill()  # This exact process was created above; no shared service stops.
        await asyncio.wait_for(first.wait(),10)
        with store.db(True) as db:
            row=db.execute('SELECT status,execution_epoch FROM eval_jobs WHERE id=?',(job_id,)).fetchone()
            assert row['status']=='running' and row['execution_epoch']==1
            # Simulate the elapsed lease only in this killed worker's unique job.
            now=db.now();db.execute('UPDATE eval_jobs SET lease_until=?,budget_tick=? WHERE id=?',(now-1,now-2,job_id))
        second=await start('recover')
        try:
            await asyncio.wait_for(second.communicate(),25)
            assert second.returncode==0
        finally:
            if second.returncode is None:second.kill();await second.wait()
        result=service.get(Principal('crash-test','operator','support'),job_id)
        assert result['status']=='completed'
        assert result['result']['evaluation_errors']==(1 if phase=='uncertain' else 0)
        with store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM telemetry WHERE kind='synthetic_paid'").fetchone()[0]==1
            assert db.execute('SELECT execution_epoch FROM eval_jobs WHERE id=?',(job_id,)).fetchone()[0]==2
    finally:
        if first.returncode is None:first.kill();await first.wait()
        await service.close()
