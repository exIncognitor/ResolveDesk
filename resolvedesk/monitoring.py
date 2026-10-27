"""Scoped rolling health, persistent cumulative metrics and fenced alert delivery."""
from __future__ import annotations

import asyncio
import json
import math
import os
import statistics
import uuid

import httpx

from resolvedesk.metrics import BUCKETS,initialize,write_sample,project_attempts
from resolvedesk.store import encode


class Monitor:
    def __init__(self,store,window=300,min_samples=3):
        self.store,self.window,self.min_samples=store,window,min_samples
        self.task=None;self.worker_id=uuid.uuid4().hex
        self.policy={'agent_success_rate':.90,'tool_success_rate':.95,'agent_avg_ms':3000,'tool_avg_ms':5000,
            'run_p95_ms':30000,'z_window':60,'z_min_samples':30,'z_sensitivity':2.5,'fallback_penalty':.5}
        with store.database.schema_transaction() as db:
            initialize(db)
            db.executescript('''
                CREATE TABLE IF NOT EXISTS desk_alerts(id TEXT PRIMARY KEY,tenant TEXT,rule_key TEXT,state TEXT,
                    payload TEXT,created REAL,updated REAL,UNIQUE(tenant,rule_key));
                CREATE TABLE IF NOT EXISTS alert_outbox(id TEXT PRIMARY KEY,tenant TEXT,alert_id TEXT,event TEXT,
                    payload TEXT,state TEXT,attempts INTEGER,next_attempt REAL,error TEXT,created REAL);
            ''')
            for column,declaration in (('execution_owner','TEXT'),('lease_until','DOUBLE PRECISION NOT NULL DEFAULT 0')):
                if not db.has_column('alert_outbox',column):db.execute('ALTER TABLE alert_outbox ADD COLUMN '+column+' '+declaration)

    def record(self,tenant,kind,name,success,seconds=0,**scope):
        with self.store.db(True) as db:
            if db.backend=='postgresql':db.execute("SET LOCAL lock_timeout='100ms'")
            return write_sample(db,tenant,kind,name,success,seconds,**scope)

    def safe_record(self,*args,**kwargs):
        try:return self.record(*args,**kwargs)
        except Exception:return False

    def _stats(self,samples):
        times=sorted(r['seconds'] for r in samples if r['measured'])
        n=len(samples);success=sum(r['success'] for r in samples)
        statuses={}
        for row in samples:
            status=row['status'] or ('success' if row['success'] else 'failure')
            statuses[status]=statuses.get(status,0)+1
        consecutive=0
        for row in samples:
            if row['success']:break
            consecutive+=1
        result={'total':n,'success':success,'success_rate':success/n if n else None,'latency_samples':len(times),
            'avg_ms':sum(times)/len(times)*1000 if times else None,'p95_ms':times[max(0,(len(times)*95+99)//100-1)]*1000 if times else None,
            'status_counts':statuses,'consecutive_fails':consecutive,'anomalies':[]}
        # Compare new data with previous data, not with a baseline containing itself.
        values=[r['seconds'] for r in samples if r['measured']][:self.policy['z_window']+1]
        if len(values)>=self.policy['z_min_samples']+1:
            baseline=values[1:];mean=statistics.mean(baseline)
            deviation=max(statistics.stdev(baseline),abs(mean)*.01,.001)
            score=(values[0]-mean)/deviation
            if score>self.policy['z_sensitivity']:result['anomalies'].append({'metric':'latency','value':values[0],'mean':mean,'z_score':score,'baseline_samples':len(baseline)})
        if len(samples)>=self.policy['z_min_samples']+5:
            previous=[1-r['success'] for r in samples[5:65]]
            value=sum(1-r['success'] for r in samples[:5])/5
            mean=statistics.mean(previous);deviation=max(statistics.stdev(previous),.01)
            score=(value-mean)/deviation
            if score>self.policy['z_sensitivity']:result['anomalies'].append({'metric':'failure_rate','value':value,'mean':mean,'z_score':score,'baseline_samples':len(previous)})
        return result

    def penalty(self,stats):
        if stats['total']<self.min_samples:return 0.
        failure=max(0,(self.policy['agent_success_rate']-stats['success_rate'])/self.policy['agent_success_rate'])*.9
        latency=max(0,1-self.policy['agent_avg_ms']/stats['avg_ms'])*.9 if stats['avg_ms'] else 0.
        return min(.9,max(failure,latency))

    def _groups(self,rows):
        buckets={};instances={}
        for row in rows:
            buckets.setdefault((row['kind'],row['name']),[]).append(row)
            if row['kind']=='agent':instances.setdefault((row['name'],row['instance']),[]).append(row)
        groups={kind:{} for kind in ('agent','tool','run','retrieval')}
        for (kind,name),samples in buckets.items():groups.setdefault(kind,{})[name]=self._stats(samples)
        per_instance={}
        for (name,identity),samples in instances.items():per_instance.setdefault(name,{})[identity]=self._stats(samples)
        return groups,per_instance

    def snapshot(self,p):
        if p.role=='support':return self.refresh(p.tenant)
        with self.store.db() as db:
            rows=db.execute('SELECT * FROM telemetry WHERE tenant=? AND owner=? AND created>=? ORDER BY seq DESC LIMIT 10000',(p.tenant,p.user,db.now()-self.window)).fetchall()
            count=db.execute('SELECT COUNT(*) FROM execution_attempts WHERE tenant=? AND owner=? AND finished IS NOT NULL',(p.tenant,p.user)).fetchone()[0]
            conversations=db.execute('SELECT COUNT(*) FROM runs WHERE tenant=? AND owner=?',(p.tenant,p.user)).fetchone()[0]
        groups,instances=self._groups(rows)
        return {'agent_stats':groups['agent'],'tool_stats':groups['tool'],'run_stats':groups['run'],'retrieval_stats':groups['retrieval'],
            'instance_stats':instances,'active_alerts':[],'suggestions':[],'total_requests':count,'total_conversations':conversations,'scope':'own_observed_executions'}

    def refresh(self,tenant):
        with self.store.db(True) as db:
            if db.backend=='postgresql':db.execute("SET LOCAL lock_timeout='100ms'")
            if db.backend=='postgresql':db.execute('SELECT pg_advisory_xact_lock(hashtextextended(?,0))',('resolvedesk-monitor:'+tenant,))
            project_attempts(db,tenant)
            now=db.now()
            rows=db.execute('SELECT * FROM telemetry WHERE tenant=? AND created>=? ORDER BY seq DESC LIMIT 10000',(tenant,now-self.window)).fetchall()
            groups,instances=self._groups(rows);triggered={}
            for kind,values in groups.items():
                for name,stats in values.items():
                    if stats['total']<self.min_samples:continue
                    target=self.policy['tool_success_rate'] if kind=='tool' else self.policy['agent_success_rate']
                    rules=[]
                    if stats['success_rate']<target:rules.append(('failure','failure_rate',f'窗口成功率低于 {target:.0%}'))
                    threshold=self.policy['tool_avg_ms'] if kind=='tool' else self.policy['agent_avg_ms']
                    if kind in {'agent','tool'} and stats['avg_ms'] and stats['avg_ms']>threshold:rules.append(('latency','latency',f'窗口平均耗时超过 {threshold}ms'))
                    if kind in {'run','retrieval'} and stats['p95_ms'] and stats['p95_ms']>self.policy['run_p95_ms']:rules.append(('latency','latency','窗口 P95 超过30秒'))
                    for anomaly in stats['anomalies']:rules.append(('z_'+anomaly['metric'],'z_score',f'{anomaly["metric"]} 的 Z-score={anomaly["z_score"]:.2f}，超过 {self.policy["z_sensitivity"]}'))
                    for suffix,rule,message in rules:
                        triggered[f'{kind}:{name}:{suffix}']={'kind':kind,'name':name,'rule':rule,'message':message,'stats':stats}
            existing={r['rule_key']:r for r in db.execute('SELECT * FROM desk_alerts WHERE tenant=?',(tenant,))}
            for key in existing.keys()|triggered.keys():
                previous=existing.get(key);state='active' if key in triggered else 'resolved'
                payload=triggered.get(key) or json.loads(previous['payload'])
                if previous is None or previous['state']!=state:
                    alert_id=previous['id'] if previous else uuid.uuid4().hex
                    db.execute('INSERT INTO desk_alerts VALUES(?,?,?,?,?,?,?) ON CONFLICT(tenant,rule_key) DO UPDATE SET state=excluded.state,payload=excluded.payload,updated=excluded.updated',(alert_id,tenant,key,state,encode(payload),now,now))
                    event={'alert_id':alert_id,'state':state,'rule_key':key,**payload}
                    db.execute("INSERT INTO alert_outbox(id,tenant,alert_id,event,payload,state,attempts,next_attempt,error,created) VALUES(?,?,?,?,?,'pending',0,?,NULL,?)",(uuid.uuid4().hex,tenant,alert_id,state,encode(event),now,now))
                elif state=='active':db.execute('UPDATE desk_alerts SET payload=?,updated=? WHERE tenant=? AND rule_key=?',(encode(payload),now,tenant,key))
            alerts=[dict(r,payload=json.loads(r['payload'])) for r in db.execute("SELECT * FROM desk_alerts WHERE tenant=? AND state='active'",(tenant,))]
            count=db.execute("SELECT COALESCE(SUM(total),0) FROM metric_totals WHERE tenant=? AND kind='run'",(tenant,)).fetchone()[0]
            conversations=db.execute('SELECT COUNT(*) FROM runs WHERE tenant=?',(tenant,)).fetchone()[0]
            outbox=[dict(r) for r in db.execute('SELECT event,state,attempts,error,created FROM alert_outbox WHERE tenant=? ORDER BY created DESC LIMIT 20',(tenant,))]
            runtime=[dict(r,payload=json.loads(r['payload'])) for r in db.execute('SELECT * FROM monitor_tool_health WHERE tenant=? AND seen>=?',(tenant,now-60))]
        penalties={name:self.penalty(stats) for name,stats in groups['agent'].items() if self.penalty(stats)>0}
        instance_penalties={identity:self.penalty(stats) for values in instances.values() for identity,stats in values.items() if self.penalty(stats)>0}
        suggestions=[]
        for alert in alerts:
            item=alert['payload'];kind,name=item['kind'],item['name']
            action='核对最近错误及依赖状态，等待一次受控恢复探测。' if item['rule']=='failure_rate' else '检查该节点模型、知识检索与工具耗时；核对超时和并发限制。'
            suggestions.append({'title':f'{kind}/{name}: {item["rule"]}','detail':item['message'],'action':action,'priority':9 if item['rule']=='failure_rate' else 6})
        for tool in runtime:
            if tool['payload']['circuit_state']!='closed':suggestions.append({'title':tool['name']+' 熔断/探测中','detail':tool['payload']['circuit_state'],'action':'核对连接与依赖健康，等待冷却后唯一只读探测；不盲目重放写入。','priority':9})
        return {'agent_stats':groups['agent'],'tool_stats':groups['tool'],'run_stats':groups['run'],'retrieval_stats':groups['retrieval'],
            'instance_stats':instances,'runtime_tools':runtime,'active_alerts':alerts,'routing_penalties':penalties,'instance_penalties':instance_penalties,
            'suggestions':suggestions,'total_requests':count,'total_conversations':conversations,'window_seconds':self.window,'min_samples':self.min_samples,
            'policy':self.policy,'window_sample_limit':10000,'window_sample_cap_reached':len(rows)==10000,
            'notifications':outbox,'webhook_configured':bool(os.getenv('RESOLVEDESK_ALERT_WEBHOOK'))}

    def publish_tools(self,tenant,worker,tools):
        with self.store.db(True) as db:
            if db.backend=='postgresql':db.execute("SET LOCAL lock_timeout='100ms'")
            for name,value in tools.items():
                db.execute('INSERT INTO monitor_tool_health VALUES(?,?,?,?,?) ON CONFLICT(tenant,worker,name) DO UPDATE SET payload=excluded.payload,seen=excluded.seen',(tenant,worker,name,encode(value),db.now()))

    async def deliver(self):
        endpoint=os.getenv('RESOLVEDESK_ALERT_WEBHOOK','')
        if not endpoint:return
        for _ in range(10):
            with self.store.db(True) as db:
                now=db.now()
                sql="SELECT * FROM alert_outbox WHERE (state='pending' AND next_attempt<=?) OR (state='sending' AND lease_until<=?) ORDER BY created LIMIT 1"
                if db.backend=='postgresql':sql+=' FOR UPDATE SKIP LOCKED'
                message=db.execute(sql,(now,now)).fetchone()
                if not message:break
                if message['attempts']>=3:
                    db.execute("UPDATE alert_outbox SET state='failed',error='delivery_outcome_unknown' WHERE id=?",(message['id'],));continue
                owner=uuid.uuid4().hex
                db.execute("UPDATE alert_outbox SET state='sending',attempts=attempts+1,execution_owner=?,lease_until=? WHERE id=?",(owner,now+15,message['id']))
            try:
                async with httpx.AsyncClient(timeout=5,follow_redirects=False,trust_env=False) as client:
                    response=await client.post(endpoint,json=json.loads(message['payload']),headers={'Idempotency-Key':message['id']})
                    response.raise_for_status()
                state,error='delivered',None
            except asyncio.CancelledError:raise
            except Exception as exc:state,error=('failed' if message['attempts']>=2 else 'pending'),type(exc).__name__
            with self.store.db(True) as db:
                db.execute("UPDATE alert_outbox SET state=?,next_attempt=?,error=?,execution_owner=NULL,lease_until=0 WHERE id=? AND state='sending' AND execution_owner=? AND lease_until>?",(state,db.now()+30,error,message['id'],owner,db.now()))

    async def _loop(self):
        while True:
            try:
                with self.store.db() as db:tenants=[r[0] for r in db.execute("SELECT DISTINCT tenant FROM telemetry UNION SELECT DISTINCT tenant FROM desk_alerts WHERE state='active' UNION SELECT DISTINCT tenant FROM execution_attempts WHERE finished IS NOT NULL AND projected=0")]
                for tenant in tenants:self.refresh(tenant)
                await self.deliver()
            except asyncio.CancelledError:raise
            except Exception:pass
            await asyncio.sleep(5)

    def start(self):
        if self.task is None:self.task=asyncio.create_task(self._loop())

    async def close(self):
        if self.task:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True);self.task=None

    def prometheus(self,p):
        snapshot=self.snapshot(p)
        lines=['# TYPE resolvedesk_requests_total counter',f'resolvedesk_requests_total {snapshot["total_requests"]}',
            '# TYPE requests_total counter',f'requests_total {snapshot["total_requests"]}',
            '# TYPE resolvedesk_conversations_total counter',f'resolvedesk_conversations_total {snapshot["total_conversations"]}',
            '# TYPE resolvedesk_active_alerts gauge',f'resolvedesk_active_alerts {len(snapshot["active_alerts"])}',
            '# TYPE resolvedesk_executions_total counter','# TYPE resolvedesk_execution_duration_seconds histogram',
            '# TYPE agent_latency_ms histogram','# TYPE chat_latency_ms histogram',
            '# TYPE agent_success_rate gauge','# TYPE tool_success_rate gauge']
        with self.store.db() as db:totals=[dict(r) for r in db.execute('SELECT * FROM metric_totals WHERE tenant=? ORDER BY kind,name,instance,status',(p.tenant,))]
        legacy={}
        for row in totals:
            fields={k:row[k] for k in ('kind','name','instance','status')}
            labels=','.join(k+'='+json.dumps(v) for k,v in fields.items())
            lines.append(f'resolvedesk_executions_total{{{labels}}} {row["total"]}')
            counts=json.loads(row['buckets'])
            for bound,count in zip(BUCKETS,counts,strict=True):lines.append(f'resolvedesk_execution_duration_seconds_bucket{{{labels},le="{bound:g}"}} {count}')
            lines.extend([f'resolvedesk_execution_duration_seconds_bucket{{{labels},le="+Inf"}} {row["measured"]}',f'resolvedesk_execution_duration_seconds_count{{{labels}}} {row["measured"]}',f'resolvedesk_execution_duration_seconds_sum{{{labels}}} {row["seconds"]}'])
            if row['kind'] in {'agent','run'}:
                key=('agent_latency_ms','agent',row['name']+':'+row['instance']) if row['kind']=='agent' else ('chat_latency_ms','status',row['status'])
                acc=legacy.setdefault(key,{'count':0,'sum':0,'buckets':[0]*len(BUCKETS)})
                acc['count']+=row['measured'];acc['sum']+=row['seconds']*1000
                acc['buckets']=[a+b for a,b in zip(acc['buckets'],counts,strict=True)]
        for (name,label,value),acc in legacy.items():
            labels=label+'='+json.dumps(value)
            for bound,count in zip(BUCKETS,acc['buckets'],strict=True):lines.append(f'{name}_bucket{{{labels},le="{bound*1000:g}"}} {count}')
            lines.extend([f'{name}_bucket{{{labels},le="+Inf"}} {acc["count"]}',f'{name}_count{{{labels}}} {acc["count"]}',f'{name}_sum{{{labels}}} {acc["sum"]}'])
        for kind in ('agent','tool','run','retrieval'):
            for name,stats in snapshot[kind+'_stats'].items():
                labels=f'kind={json.dumps(kind)},name={json.dumps(name)}'
                for metric in ('total','success','p95_ms'):
                    if stats[metric] is not None:lines.append(f'resolvedesk_window_{metric}{{{labels}}} {stats[metric]}')
                if kind=='tool':lines.append(f'tool_success_rate{{tool={json.dumps(name)}}} {stats["success_rate"]}')
        for role,instances in snapshot['instance_stats'].items():
            for instance,stats in instances.items():lines.append(f'agent_success_rate{{agent={json.dumps(role+":"+instance)}}} {stats["success_rate"]}')
        return '\n'.join(lines)+'\n'
