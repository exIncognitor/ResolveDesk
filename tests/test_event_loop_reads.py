"""Driver waits must release the event loop, with no timing-speed assertion."""
import asyncio
import hashlib
import threading
import httpx
import pytest
from resolvedesk.agent import DeskAgent
from resolvedesk.app import create_app
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.store import Store
from test_resolvedesk import USER,client_with,response


@pytest.mark.asyncio
@pytest.mark.parametrize('path',['events','get_ticket'])
async def test_native_read_wait_allows_other_coroutines_to_progress(tmp_path,path):
    store=Store(tmp_path/'loop.sqlite3');kb=HybridKnowledge(store)
    run,_=store.create_run(USER,'synthetic source ticket','loop-source-ticket');store.claim(USER,run['id'])
    store.prepare(USER,run['id'],{'summary':'synthetic','description':'synthetic','category':'network','priority':'normal'},[])
    current=store.get_run(USER,run['id']);ticket=store.confirm(USER,run['id'],current['approval'])
    model=client_with(response(tool='get_ticket',args={'ticket_id':ticket['id']}),response('已读取当前授权工单。'))
    agent=DeskAgent(store,kb,model)
    app=create_app(store,kb,agent,{hashlib.sha256(b'loop-token').hexdigest():vars(USER)})
    entered=threading.Event();release=threading.Event();timed_out=[]
    original=getattr(store,path)
    def driver_wait(*args,**kwargs):
        entered.set()
        if not release.wait(3):timed_out.append(True)
        return original(*args,**kwargs)
    setattr(store,path,driver_wait)
    async def other_request():
        assert await asyncio.to_thread(entered.wait,3)
        await asyncio.sleep(0)
        release.set()
    try:
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://loop',headers={'Authorization':'Bearer loop-token'}) as http:
            heartbeat=asyncio.create_task(other_request())
            if path=='events':
                result=await http.get('/runs/'+run['id']+'/events',params={'follow':False});assert result.status_code==200
            else:
                new,_=store.create_run(USER,'查询工单'+ticket['id'],'loop-read-ticket')
                await agent.execute(USER,new['id']);assert store.get_run(USER,new['id'])['status']=='completed'
            await heartbeat
            assert not timed_out
    finally:release.set();store.close()
