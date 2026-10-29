"""Current ASGI quality evaluation, observed intents, per-turn Judge and recovery."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import secrets
import statistics
import time

import httpx

from evaluation.evaluator import LLMJudge
from resolvedesk.eval_metrics import assertions,intent_report,measure_intent
from resolvedesk.eval_models import EvalDataset
from resolvedesk.eval_sandbox import EvalSandbox,freeze
from resolvedesk.eval_state import EvalState
from resolvedesk.store import Principal,digest,encode
from resolvedesk.knowledge_governance import publication_identity

ROOT=Path(__file__).resolve().parents[1]
CORPUS=ROOT/'evaluation'/'it_service_cases.json'
SCORES=('relevance','accuracy','completeness','helpfulness')


def default_dataset():
    corpus=json.loads(CORPUS.read_text(encoding='utf-8'))
    return EvalDataset.model_validate({k:v for k,v in corpus.items() if k!='split'}|{'title':'IT 合成开发集',
        'assets':[{'id':'LT-1001','owner':'learner','model':'合成开发笔记本'}]}).model_dump()


def average_scores(items):
    return {key:statistics.mean(d['scores'][key] for d in items if d.get('scores')) for key in SCORES} if any(d.get('scores') for d in items) else {}


def suite_identity(manifest,limit,use_judge,embedding_identity,judge):
    # Regressions compare engine revisions under the same measurement contract.
    # The execution revision stays separate, and still fences same-job resume.
    return digest({'schema':'active-quality-v3-publications','dataset':manifest['dataset'],'limit':limit,'judge':use_judge,'threshold':.75,
        'judge_prompt':judge.JUDGE_PROMPT,'few_shot':judge.few_shot_prompt(),'backend':manifest['backend'],
        'embedding':embedding_identity,'model_identity':manifest.get('model_identity'),'evidence':manifest['evidence'],'publications':publication_identity(manifest.get('publications',[]))})


class QualityRunner:
    def __init__(self,store,knowledge,client_factory=None,*,coordination=None):
        self.store,self.knowledge,self.client_factory,self.coordination=store,knowledge,client_factory,coordination

    async def run(self,p,job_id,limit,use_judge,progress,*,dataset=None,state=None):
        from core.model_client import ModelClient, model_identity
        from resolvedesk.agent import DeskAgent
        from resolvedesk.app import create_app
        from resolvedesk.services import DeskServices
        if dataset is None:
            if p.tenant!='demo':raise ValueError('Non-demo evaluation requires an explicit dataset')
            dataset=default_dataset()
        dataset=EvalDataset.model_validate(dataset).model_dump()
        state=state or EvalState(self.store,job_id)
        state.embedding_identity=getattr(self.knowledge.embedding,'identity',None)
        state.redis_namespace=getattr(self.coordination,'namespace',None)
        client=(self.client_factory.for_actor(p,'evaluation:'+job_id) if callable(getattr(type(self.client_factory),'for_actor',None))
                else self.client_factory() if self.client_factory else ModelClient(timeout=25))
        state.model_identity=model_identity(client)
        try:manifest=freeze(state,p,dataset,ROOT)
        except BaseException:
            await client.close()
            raise
        dataset=manifest['dataset']
        cases=dataset['dialog_cases'][:limit]
        sandbox=EvalSandbox(self.store,self.knowledge,manifest,ROOT/'.codex-runtime'/('quality-'+manifest['identity']),self.coordination)
        agent=None
        details,intents=[],[]
        try:
            await sandbox.open(p)
            store,kb=sandbox.store,sandbox.kb
            services=DeskServices(store,kb,client,coordination=sandbox.coordination)
            agent=DeskAgent(store,kb,client,services,coordination=sandbox.coordination)
            actors={c['user_id']:Principal(p.tenant,c['user_id']) for c in cases}
            actors['support']=Principal(p.tenant,'support','support')
            tokens={name:secrets.token_urlsafe(32) for name in actors}
            accounts={hashlib.sha256(tokens[name].encode()).hexdigest():vars(actor) for name,actor in actors.items()}
            api=create_app(store,kb,agent,accounts)
            judge=LLMJudge(client,getattr(client,'model','deepseek-flash'),max_attempts=1)
            judge._max_tokens=512
            suite_id=suite_identity(manifest,limit,use_judge,getattr(kb.embedding,'identity',None),judge)
            total=len(dataset['intent_cases'])+len(cases)
            await services.start()
            async with api.router.lifespan_context(api),httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://quality.local',timeout=120) as http:
                async def request(actor,method,path,payload=None):
                    response=await http.request(method,path,json=payload,headers={'Authorization':'Bearer '+tokens[actor]})
                    response.raise_for_status()
                    return response.json()

                async def wait(actor,run_id,key):
                    async with asyncio.timeout(110):
                        while True:
                            run=store.get_turn(actors[actor],run_id,key)
                            if run['status'] not in {'running','queued'}:return run
                            await asyncio.sleep(.05)

                def capture(actor,run):
                    with store.db() as db:
                        events=[dict(r,payload=json.loads(r['payload'])) for r in db.execute('SELECT * FROM events WHERE run_id=? AND seq>? AND seq<=? ORDER BY seq',(run['id'],run['event_cursor'],run['event_end']))]
                    return {'run':run,'events':events,'entities':services.entities.get(actors[actor],run['id'])}

                for case in dataset['intent_cases']:
                    try:
                        async def measure():return await measure_intent(services.roles._intent_recognizer,case)
                        result=await state.unit('intent:'+case['id'],measure)
                    except asyncio.CancelledError:raise
                    except Exception as exc:
                        result={'id':case['id'],'expected':case['expected_intent'],'prediction':None,'sources':{},'passed':False,'error':type(exc).__name__}
                    intents.append(result)
                    progress(len(intents),total)

                conversations={}
                for index,case in enumerate(cases):
                    actor=case['user_id']
                    group=(actor,case.get('conv_id') or case['id'])
                    run_id=conversations.get(group)
                    cached=state.result('dialog:'+case['id']+':result')
                    if cached:
                        details.append(cached)
                        if cached.get('run_id'):conversations[group]=cached['run_id']
                        progress(len(intents)+index+1,total)
                        continue
                    detail={'test_id':case['id'],'passed':False,'metadata':{},'scores':{},'turns':[]}
                    try:
                        for turn_index,turn in enumerate(case['turns']):
                            key='quality-'+digest([job_id,case['id'],turn_index])[:48]
                            async def execute():
                                before={t['id'] for t in store.list_tickets(actors[actor])}
                                if run_id:run=await request(actor,'POST',f'/runs/{run_id}/reply',{'message':turn['message'],'request_key':key})
                                else:run=await request(actor,'POST','/runs',{'message':turn['message'],'request_key':key})
                                value=capture(actor,await wait(actor,run['id'],key))
                                return value|{'tickets_before':sorted(before),'tickets_after':sorted(t['id'] for t in store.list_tickets(actors[actor]))}
                            def recover():
                                with store.db() as db:
                                    row=db.execute('SELECT run_id,response,status FROM turn_receipts WHERE tenant=? AND owner=? AND request_key=?',(p.tenant,actor,key)).fetchone()
                                if row and row['response'] and row['status'] not in {'queued','running','paused','failed'}:
                                    return capture(actor,json.loads(row['response']))
                            observed=await state.unit(f'dialog:{case["id"]}:turn:{turn_index}',execute,recover)
                            run=observed['run'];run_id=run['id'];conversations[group]=run_id
                            checks,routing,tools=assertions(turn,run,observed['events'],observed['entities'])
                            checks['no_unconfirmed_write']=set(observed['tickets_after'])<=set(observed['tickets_before']) if 'tickets_before' in observed else not run.get('ticket_id')
                            judged={}
                            if use_judge:
                                async def score():
                                    history=[{'user':c['message'],'assistant':d['answer']} for c,d in zip(case['turns'][:turn_index],detail['turns'])]
                                    context={'history':history,'reference':turn.get('reference') or (case['reference_answer'] if turn_index==len(case['turns'])-1 else ''),
                                        'evidence':turn.get('evidence') or case['evidence'],'authorized_knowledge':[{k:r[k] for k in ('document_id','section','content')} for r in manifest['evidence'] if r['document_id'] in case['documents']],
                                        'observed_status':run['status']}
                                    return asdict(await judge.judge(turn['message'],run['answer'],encode(context)))
                                try:judged=await state.unit(f'dialog:{case["id"]}:judge:{turn_index}',score)
                                except asyncio.CancelledError:raise
                                except Exception as exc:judged={'judge_failed':True,'error':type(exc).__name__}
                            scores={k:judged[k] for k in SCORES} if judged and not judged.get('judge_failed') else {}
                            passed=all(checks.values()) and (not use_judge or bool(scores) and statistics.mean(scores.values())>=.75)
                            detail['turns'].append({'turn':turn_index+1,'answer':run['answer'],'status':run['status'],'latency_ms':run.get('latency_ms'),'checks':checks,'routing':routing,'tools':tools,'judge':judged,'scores':scores,'deterministic_passed':all(checks.values()),'passed':passed})
                        final_checks,_,_=assertions(case,run,observed['events'],observed['entities'])
                        retrieved={kb.citation(actors[actor],cid)['document_id'] for t in detail['turns'] for cid in re.findall(r'C-[a-f0-9]{24}',t['answer'])}
                        with store.db() as db:
                            retrievals=[json.loads(r[0]) for r in db.execute("SELECT payload FROM events WHERE run_id=? AND kind='retrieval'",(run_id,))]
                        evidence={kb.citation(actors[actor],cid)['document_id'] for r in retrievals for cid in r.get('citations',[])}
                        final_checks['retrieved_evidence']=set(case['documents'])<=evidence
                        if case['documents'] and not case['workflow']:final_checks['cited_answer']=bool(retrieved&set(case['documents']))
                        lifecycle=None
                        if case['workflow']:
                            final_checks['workflow']=bool(run.get('draft')) and run['draft'].get('workflow')==case['workflow']
                            final_checks['expected_fields']=all((run.get('draft') or {}).get(k)==v for k,v in case['expected_fields'].items())
                            final_checks['prepare_tool']='prepare_ticket' in detail['turns'][-1]['tools']
                            if run['status']=='awaiting_confirmation' and final_checks['workflow']:
                                async def confirm():
                                    ticket=await request(actor,'POST',f'/runs/{run_id}/confirm',{'approval':run['approval']})
                                    same=await request(actor,'POST',f'/runs/{run_id}/confirm',{'approval':run['approval']})
                                    if ticket['id']!=same['id']:raise ValueError('Confirmation is not idempotent')
                                    return ticket
                                def recover_ticket():
                                    current=store.get_run(actors[actor],run_id)
                                    return {'id':current['ticket_id']} if current.get('ticket_id') else None
                                ticket=await state.unit('dialog:'+case['id']+':confirm',confirm,recover_ticket)
                                final_checks['idempotent_confirmation']=True
                                lifecycle=await request(actor,'GET','/service-cases/'+ticket['id'])
                                actions={'access_request':['approve','provision'],'device_repair':['triage','assign','start','resolve','reopen','resolve','accept'],'license_request':['approve','reserve','allocate']}[case['workflow']]
                                for step,action in enumerate(actions):
                                    operator=actor if action in {'reopen','accept'} else 'support'
                                    action_key='quality-'+digest([job_id,case['id'],step])[:48]
                                    async def act():return await request(operator,'POST',f'/service-cases/{ticket["id"]}/actions',{'action':action,'version':lifecycle['version'],'request_key':action_key,'note':'合成评测操作：'+action,'assignee':'support' if action=='assign' else ''})
                                    def recover_action():
                                        with store.db() as db:r=db.execute('SELECT response FROM service_actions WHERE tenant=? AND actor=? AND ticket_id=? AND request_key=?',(p.tenant,operator,ticket['id'],action_key)).fetchone()
                                        return json.loads(r[0]) if r else None
                                    lifecycle=await state.unit(f'dialog:{case["id"]}:action:{step}',act,recover_action)
                                expected={'access_request':'fulfilled','device_repair':'closed','license_request':'allocated'}[case['workflow']]
                                final_checks['business_final_state']=lifecycle['status']==expected
                                context=await request(actor,'GET','/service-catalog')
                                if expected=='fulfilled':
                                    grants=[g for g in context['my_grants'] if g['resource']==case['expected_fields'].get('resource_id')]
                                    final_checks['persisted_business_effect']=len(grants)==1 and grants[0]['level']==case['expected_fields'].get('access_level') and abs((grants[0]['expires']-time.time())-case['expected_fields'].get('duration_days',0)*86400)<60
                                elif expected=='allocated':
                                    allocations=[a for item in context['licenses'] for a in item.get('my_allocations',[]) if a['ticket_id']==ticket['id'] and a['product']==case['expected_fields'].get('product') and a['state']=='allocated']
                                    final_checks['persisted_business_effect']=len(allocations)==1 and abs((allocations[0]['expires']-time.time())-case['expected_fields'].get('duration_days',0)*86400)<60
                                else:final_checks['persisted_business_effect']=any(a['id']==case['expected_fields'].get('asset_id') and a['status']=='available' for a in context['my_assets'])
                        detail.update(run_id=run_id,answer=run['answer'],checks=final_checks,lifecycle=lifecycle,draft=run.get('draft'),evidence_documents=sorted(evidence),cited_documents=sorted(retrieved))
                        detail['scores']=average_scores(detail['turns'])
                        detail['metadata']['judge_failed']=any(t['judge'].get('judge_failed') for t in detail['turns'])
                        detail['passed']=all(t['passed'] for t in detail['turns']) and all(final_checks.values())
                        detail['deterministic_passed']=all(t['deterministic_passed'] for t in detail['turns']) and all(final_checks.values())
                    except asyncio.CancelledError:raise
                    except Exception as exc:detail['metadata']['execution_error']=type(exc).__name__
                    detail['turn_states']=[t['status'] for t in detail['turns']]
                    state.result('dialog:'+case['id']+':result',detail)
                    details.append(detail)
                    progress(len(intents)+index+1,total)
        finally:
            if agent:await agent.close()
            else:await client.close()
            await sandbox.close()
        valid=[d for d in details if not d['metadata'].get('judge_failed') and not d['metadata'].get('execution_error')]
        intent=intent_report(intents)
        average=average_scores([t for d in valid for t in d['turns']])
        average.update(intent_accuracy=intent['accuracy'],intent_macro_f1=intent['macro_f1'],business_check_rate=sum(all(d.get('checks',{'missing':False}).values()) for d in details)/len(details) if details else None)
        passed=sum(d['passed'] for d in details)
        errors=len(details)-len(valid)+intent['evaluation_errors']
        requested_total=len(intents)+len(details)
        return {'kind':'active_service_quality','suite_id':suite_id,'corpus_version':dataset['version'],'provenance':dataset['provenance'],
            'suite_schema':'active-quality-v3-publications','engine_identity':digest(manifest['engine_sources']),'model_configuration':manifest.get('model_identity'),
            'status':'partial' if errors else 'complete','total':len(details),'valid_total':len(valid),'passed':passed,
            'pass_rate':passed/len(details) if details else 0,'valid_pass_rate':passed/len(valid) if valid else 0,
            'evaluation_errors':errors,'deterministic_passed':sum(d.get('deterministic_passed',False) for d in details),'avg_scores':average,'intent':intent,'results':details,'regressions':[],
            'requested_total':requested_total,'overall_pass_rate':(passed+sum(r['passed'] for r in intents))/requested_total if requested_total else 0,
            'regression_note':'需人工选定相同数据、引擎、知识版本与评分配置的有效基线。','judge_enabled':use_judge,
            'recommendations':['合成开发集不能证明生产效果；同一 Flash 生成和评分存在评判偏差。','逐轮独立 Judge，任一轮不通过即整段不通过。中断后的不确定付费单元禁止自动重放。'],
            'sandbox':{'backend':manifest['backend'],'identity':manifest['identity'],'isolated':True},'requested_intents':len(intents),'requested_turns':sum(len(c['turns']) for c in cases)}
