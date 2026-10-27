"""Real local integration acceptance. Synthetic transcript seeds are labeled."""
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    import httpx
    from resolvedesk.store import Store,Principal
    access=json.loads((ROOT/'.codex-runtime/access.json').read_text(encoding='utf-8'))
    p=Principal('demo','learner')
    checks=[]
    with httpx.Client(base_url='http://127.0.0.1:8080',timeout=15,headers={'Authorization':'Bearer '+access['learner']['token']}) as c:
        def req(method,path,data=None):
            r=c.request(method,path,json=data);r.raise_for_status();return r.json()
        def wait(run_id):
            until=time.monotonic()+110
            while time.monotonic()<until:
                run=req('GET','/runs/'+run_id)
                if run['status'] not in {'queued','running'}:return run
                time.sleep(.5)
            raise TimeoutError('Integration run timed out')
        def new(message):
            return wait(req('POST','/runs',{'message':message,'request_key':'integrated-'+uuid.uuid4().hex})['id'])
        # These messages are explicit synthetic fixture data, not a claim of real user dialogue.
        store=Store(ROOT/'data/resolvedesk.sqlite3')
        seeded,_=store.create_run(p,'[集成验证样本] 我使用 Windows 11 和 IntelliJ，VPN 连接超时。','memory-fixture-'+uuid.uuid4().hex)
        for i in range(6):
            store.claim(p,seeded['id']);store.finish(p,seeded['id'],'awaiting_input',f'[合成上下文 {i}] 请补充现象。')
            store.reply(p,seeded['id'],f'[合成上下文 {i}] VPN 连接超时，普通网页正常。')
        store.pause(p,seeded['id'])
        req('POST','/runs/'+seeded['id']+'/resume')
        remembered=wait(seeded['id'])
        # If a clarification was necessary, finish the fixture with a grounded question.
        if remembered['status']=='awaiting_input':
            req('POST','/runs/'+seeded['id']+'/reply',{'message':'不用继续追问，也不要创建工单。请仅根据知识库给出 VPN 超时的初步检查建议。','request_key':'smoke-'+uuid.uuid4().hex})
            remembered=wait(seeded['id'])
        trace=req('GET','/trace/tool/'+seeded['id'])['trace']['events']
        memory=req('GET','/memory')
        checks.append({'case':'summary_profile_episode','seeded_transcript':True,'run_id':seeded['id'],'pass':remembered['status']=='completed' and memory['summaries']>0 and memory['episodes']>0 and memory['profile'].get('os')=='Windows 11','memory':memory})
        recalled=new('我之前在 Windows 系统上用什么 IDE？结合我的历史记忆回答，然后简要复述 VPN 超时应检查哪些信息。不要建单。')
        trace=req('GET','/trace/tool/'+recalled['id'])['trace']['events']
        contexts=[e['payload'] for e in trace if e['kind']=='context']
        checks.append({'case':'cross_run_memory_and_stream','run_id':recalled['id'],'pass':recalled['status'] in {'completed','awaiting_input'} and any(e['kind']=='answer_delta' for e in trace) and any(v['profile_fields'] for v in contexts),'context_events':contexts,'answer':recalled['answer']})
        composite=new('登录接口报错 401，同时订阅被重复扣款。请分别分析技术排查和账单核验的边界，不能执行任何退款或创建工单。')
        trace=req('GET','/trace/tool/'+composite['id'])['trace']['events']
        routes=[e['payload'] for e in trace if e['kind']=='routing']
        opinions=routes[-1].get('specialist_opinions',[]) if routes else []
        checks.append({'case':'parallel_specialist_agents','run_id':composite['id'],'pass':len(opinions)>=2 and all(o['success'] for o in opinions) and composite['status'] in {'completed','awaiting_input'},'routing':routes})
        mcp=new('请先调用 MCP support_checklist 工具读取 network 类故障清单，再告诉我 VPN 超时应收集哪些信息。不要建单。')
        trace=req('GET','/trace/tool/'+mcp['id'])['trace']['events']
        calls=[e['payload'] for e in trace if e['kind']=='tool_result']
        checks.append({'case':'model_calls_standard_mcp','run_id':mcp['id'],'pass':any(v.get('source')=='mcp' and v.get('success') for v in calls),'tool_results':calls})
        with httpx.Client(base_url='http://127.0.0.1:8080',timeout=15,headers={'Authorization':'Bearer '+access['support']['token']}) as support:
            job=support.post('/eval/run');job.raise_for_status();job_id=job.json()['job_id']
            until=time.monotonic()+30
            while time.monotonic()<until:
                r=support.get('/eval/jobs/'+job_id);r.raise_for_status();evaluation=r.json()
                if evaluation['status'] not in {'running','queued'}:break
                time.sleep(.2)
            checks.append({'case':'persistent_retrieval_evaluation','job_id':job_id,'pass':evaluation['status']=='completed' and evaluation['result']['total']>=8,'result':evaluation.get('result')})
            r=support.get('/system');r.raise_for_status();system=r.json()
    report={'created_utc':datetime.now(timezone.utc).isoformat(),'model':'deepseek-flash','cases':checks,'all_passed':all(v['pass'] for v in checks),'system':system}
    output=ROOT/'reports'/('integrated-real-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
    with output.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps({'all_passed':report['all_passed'],'cases':[{k:v for k,v in row.items() if k in ('case','pass','run_id','job_id')} for row in checks],'budget':system['budget'],'report':str(output)},ensure_ascii=False,indent=2))
    if not report['all_passed']:raise SystemExit(1)


if __name__=='__main__':main()
