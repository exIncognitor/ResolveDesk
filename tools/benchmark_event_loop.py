"""Free, isolated PG slow-read injection: client latency vs loop heartbeat gap."""
import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace
import uuid

ROOT=Path(__file__).resolve().parents[1]


async def main(args):
    sys.path.insert(0,str(args.engine_root.resolve()))
    os.environ['RESOLVEDESK_PAID_API_ENABLED']='false'
    from resolvedesk.app import create_app
    from resolvedesk.agent import DeskAgent
    from resolvedesk.retrieval import HybridKnowledge
    from resolvedesk.settings import ServiceSettings
    from resolvedesk.store import Store,Principal
    import httpx
    logging.disable(logging.INFO)
    if args.output.exists():raise ValueError('Refuse to overwrite evidence')
    store=Store(ServiceSettings.load(ROOT).database_url,schema='bench_v4_'+uuid.uuid4().hex)
    p=Principal('benchmark','reader');kb=HybridKnowledge(store)
    async def close():pass
    calls=[]
    async def create(**kwargs):return calls.pop(0)
    model=SimpleNamespace(messages=SimpleNamespace(create=create),close=close)
    agent=DeskAgent(store,kb,model)
    app=create_app(store,kb,agent,{hashlib.sha256(b'benchmark-only').hexdigest():vars(p)})
    report={'engine_root':str(args.engine_root),'backend':'PostgreSQL','slow_query_seconds':.12,'paid_calls':0,'schema':store.database.schema,'measurements':[],
        'note':'Controlled pg_sleep injection into current Store method, not normal throughput or production benchmark. Unique synthetic schema retained.'}
    def delay():
        with store.db() as db:db.execute('SELECT pg_sleep(?)',(.12,))
    original_events,original_ticket=store.events,store.get_ticket
    def slow_events(*a,**kw):delay();return original_events(*a,**kw)
    def slow_ticket(*a,**kw):delay();return original_ticket(*a,**kw)
    async def measure(operation):
        gaps=[];stop=asyncio.Event();ready=asyncio.Event()
        async def beat():
            previous=time.monotonic();ready.set()
            while not stop.is_set():
                await asyncio.sleep(.01);now=time.monotonic();gaps.append(now-previous);previous=now
        task=asyncio.create_task(beat());await ready.wait();await asyncio.sleep(.025)
        started=time.monotonic()
        try:await operation()
        finally:
            elapsed=time.monotonic()-started;await asyncio.sleep(.025);stop.set();await task
        return {'latency_ms':round(elapsed*1000,3),'max_heartbeat_gap_ms':round(max(gaps)*1000,3)}
    try:
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://bench',headers={'Authorization':'Bearer benchmark-only'}) as http:
            for repeat in range(3):
                run,_=store.create_run(p,'synthetic terminal','terminal-'+str(repeat));store.claim(p,run['id']);store.finish(p,run['id'],'completed','synthetic')
                store.events=slow_events
                async def read_events():
                    response=await http.get('/runs/'+run['id']+'/events',params={'follow':False});response.raise_for_status()
                report['measurements'].append({'path':'SSE events','repeat':repeat+1,**await measure(read_events)})
                store.events=original_events
                run,_=store.create_run(p,'synthetic confirmed ticket','ticket-'+str(repeat));store.claim(p,run['id'])
                store.prepare(p,run['id'],{'summary':'synthetic','description':'synthetic','category':'network','priority':'normal'},[])
                draft=store.get_run(p,run['id']);ticket=store.confirm(p,run['id'],draft['approval'])
                run,_=store.create_run(p,'查询工单'+ticket['id'],'read-'+str(repeat))
                calls.extend([SimpleNamespace(stop_reason='tool_use',usage=SimpleNamespace(input_tokens=0,output_tokens=0),content=[{'type':'tool_use','id':'read','name':'get_ticket','input':{'ticket_id':ticket['id']}}]),
                    SimpleNamespace(stop_reason='end_turn',usage=SimpleNamespace(input_tokens=0,output_tokens=0),content=[{'type':'text','text':'已读取当前授权的合成工单。'}])])
                store.get_ticket=slow_ticket
                report['measurements'].append({'path':'Agent get_ticket','repeat':repeat+1,**await measure(lambda:agent.execute(p,run['id']))})
                store.get_ticket=original_ticket
                assert store.get_run(p,run['id'])['status']=='completed'
        report['median_gap_ms']={path:statistics.median(r['max_heartbeat_gap_ms'] for r in report['measurements'] if r['path']==path) for path in ('SSE events','Agent get_ticket')}
        with args.output.open('x',encoding='utf-8') as out:json.dump(report,out,ensure_ascii=False,indent=2)
        print(json.dumps({'median_gap_ms':report['median_gap_ms'],'paid_calls':0,'report':str(args.output)}))
    finally:store.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--engine-root',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    asyncio.run(main(parser.parse_args()))
