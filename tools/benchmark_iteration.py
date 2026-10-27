"""Free isolated component benchmark, never a model/semantic-quality claim."""
import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]


class SyntheticEmbedding:
    identity='benchmark-sha256-normalized-64-v1'
    def embed(self,texts,query=False):
        result=[]
        for text in texts:
            values=[(byte-127.5)/127.5 for byte in hashlib.sha512(text.encode()).digest()]
            norm=math.sqrt(sum(v*v for v in values));result.append([v/norm for v in values])
        return result


def memory_usage():
    if sys.platform=='win32':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(name,ctypes.c_size_t) for name in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
        data=Counters();data.cb=ctypes.sizeof(data)
        current=ctypes.windll.kernel32.GetCurrentProcess;current.restype=wintypes.HANDLE
        if not ctypes.windll.psapi.GetProcessMemoryInfo(current(),ctypes.byref(data),data.cb):raise OSError('Memory counters unavailable')
        return {'rss_bytes':data.WorkingSetSize,'process_lifetime_peak_rss_bytes':data.PeakWorkingSetSize}
    import resource
    return {'process_lifetime_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)}


def percentile(values,p):
    values=sorted(values);return values[max(0,math.ceil(len(values)*p)-1)]


async def run(args):
    sys.path.insert(0,str(args.engine_root))
    from sqlalchemy import text
    from resolvedesk.settings import ServiceSettings
    from resolvedesk.store import Store,Principal,encode
    from resolvedesk.retrieval import HybridKnowledge
    from resolvedesk.reliable_retrieval import ReliableRetrieval
    from resolvedesk.coordination import Coordination
    from resolvedesk.vectors import ChromaVectors
    from tooling.tool_manager import MCPToolManager
    from core.paid_api import BudgetedAnthropic
    os.environ['RESOLVEDESK_PAID_API_ENABLED']='false'
    settings=ServiceSettings.load(args.config_root)
    if args.output.exists():raise ValueError('Refuse to overwrite benchmark evidence')
    contract={'schema':'iteration-components-v1','chunks':args.chunks,'concurrency':[1,5,10],'samples_per_phase':args.samples,
        'top_k':5,'mode':'hybrid','optimize':False,'embedding':SyntheticEmbedding.identity,
        'corpus':'100 synthetic 64-dimensional chunks per document; explicit vectors; 220-character safety-policy filler and unique case codes.',
        'phases':'cold = new process knowledge/query caches and unique requests; warm = repeat those queries using Redis/process cache. SQL/Chroma page caches are not flushed. No paid model calls.'}
    report={'contract':contract,'python':platform.python_version(),'platform':platform.platform(),'engine_root':str(args.engine_root),
        'retrieval_source_sha256':hashlib.sha256((args.engine_root/'resolvedesk/retrieval.py').read_bytes()).hexdigest(),'profiles':[]}
    user=Principal('bench','reader');embedding=SyntheticEmbedding()
    for count in args.chunks:
        identity=uuid.uuid4().hex;schema='iteration_bench_'+identity
        store=Store(settings.database_url,schema=schema)
        vectors=ChromaVectors(settings.chroma_url,embedding.identity,collection='iteration-bench-'+identity)
        kb=HybridKnowledge(store,embedding,vectors)
        rows=[];documents={}
        for index in range(count):
            document='benchdoc-'+str(index//100)
            content=('Synthetic IT VPN network policy. Never provide secrets or disable firewalls. Check client version and incident time. ' * 2)+' casecode-'+str(index).zfill(5)
            cid=hashlib.sha256(str(index).encode()).hexdigest()[:24]
            vector=embedding.embed([content])[0]
            row={'id':cid,'tenant':'bench','owner':'support','visibility':'tenant','document_id':document,'title':'合成网络排障 '+document,
                'version':'bench-v1','section':'网络安全诊断','start':index*500,'end':index*500+len(content),'content':content,'vector':encode(vector),'embedding':embedding.identity}
            rows.append(row);documents.setdefault(document,[]).append({**row,'vector':vector})
        with store.database.engine.begin() as conn:
            columns=list(rows[0]);conn.execute(text('INSERT INTO chunks('+','.join('"'+c+'"' for c in columns)+') VALUES('+','.join(':'+c for c in columns)+')'),rows)
            conn.execute(text('INSERT INTO knowledge_documents VALUES(:tenant,:document,:owner,:visibility,:version,1,:updated)'),[{'tenant':'bench','document':d,'owner':'support','visibility':'tenant','version':'bench-v1','updated':time.time()} for d in documents])
        for document,items in documents.items():vectors.replace('bench',document,items)
        for concurrency in contract['concurrency']:
            # New KB instance resets candidate lexical cache without touching SQL.
            knowledge=HybridKnowledge(store,embedding,vectors)
            cache=Coordination(settings.redis_url,'iteration-bench-'+identity+'-'+str(concurrency))
            manager=MCPToolManager(api_key='',model='deepseek-flash')
            client=BudgetedAnthropic(api_key='')
            retrieval=ReliableRetrieval(knowledge,client,manager,cache)
            try:
                for phase in ('cold','warm'):
                    slots=asyncio.Semaphore(concurrency);values=[];errors=[];hits=[];stop=asyncio.Event();lag=[]
                    async def observe_loop():
                        while not stop.is_set():
                            start=time.perf_counter();await asyncio.sleep(.01);lag.append(max(0,(time.perf_counter()-start-.01)*1000))
                    async def request(index):
                        async with slots:
                            start=time.perf_counter()
                            try:
                                result=await retrieval.search(user,'VPN casecode-'+str((index*137)%count).zfill(5),5,False)
                                hits.append(bool(result['cached']))
                                if result['errors'] or result['degraded'] or not result['items']:errors.append({'request':index,'errors':result['errors'],'degraded':result['degraded']})
                            except Exception as exc:errors.append({'request':index,'exception':type(exc).__name__})
                            finally:values.append((time.perf_counter()-start)*1000)
                    sampler=asyncio.create_task(observe_loop());started=time.perf_counter();cpu=time.process_time()
                    await asyncio.gather(*(request(index) for index in range(args.samples)))
                    elapsed=time.perf_counter()-started;cpu=time.process_time()-cpu;stop.set();await sampler
                    profile={'chunks':count,'concurrency':concurrency,'phase':phase,'requests':len(values),'p50_ms':statistics.median(values),'p95_ms':percentile(values,.95),
                        'errors':errors,'error_rate':len(errors)/len(values),'cache_hits':sum(hits),'elapsed_s':elapsed,'process_cpu_s':cpu,'event_loop_lag_p95_ms':percentile(lag,.95),**memory_usage()}
                    report['profiles'].append(profile)
                    print(json.dumps({k:v for k,v in profile.items() if k!='errors'}),flush=True)
            finally:
                await cache.close();await manager._client.close();await client.close()
        vectors.close();store.close()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as out:json.dump(report,out,ensure_ascii=False,indent=2)
    print(json.dumps({'output':str(args.output),'profiles':len(report['profiles'])}),flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--engine-root',type=Path,required=True)
    parser.add_argument('--config-root',type=Path,default=ROOT)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--samples',type=int,default=40)
    parser.add_argument('--chunks',nargs='+',type=int,default=[1000,10000])
    args=parser.parse_args()
    if args.samples<20 or any(n not in {1000,10000} for n in args.chunks):parser.error('Use >=20 samples and the fixed 1000/10000 chunk sizes')
    asyncio.run(run(args))


if __name__=='__main__':main()
