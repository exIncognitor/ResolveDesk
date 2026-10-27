"""Read-only, owner-scoped trace pages with stable event and turn cursors."""
import json

from resolvedesk.tracing import redact


def trace_page(store,p,run_id,turn_key=None,*,after=0,limit=200,through=None):
    with store.db() as db:
        row=store._run(db,p,run_id)
        current=store._turn(db,p,run_id,turn_key) if turn_key else store._snapshot(db,row)
        end=current['event_end'] if through is None else min(through,current['event_end'])
        cursor=max(after,current['event_cursor'])
        rows=db.execute('SELECT * FROM events WHERE run_id=? AND seq>? AND seq<=? ORDER BY seq LIMIT ?',(run_id,cursor,end,limit+1)).fetchall()
        more=len(rows)>limit
        events=[dict(r,payload=redact(json.loads(r['payload']))) for r in rows[:limit]]
    calls=[];seen=set();fallbacks=[]
    for event in events:
        payload=event['payload']
        if event['kind']=='tool_result':
            item={**payload,'tool_name':payload.get('tool_name') or payload.get('name'),'event_seq':event['seq']}
            calls.append(item)
            if item.get('trace_id'):seen.add(item['trace_id'])
        elif event['kind']=='specialist_result':
            for item in payload.get('tool_traces',[]):
                if item.get('event')=='agent_fallback':fallbacks.append(item);continue
                # New calls are persisted immediately; aggregated copies are
                # compatibility payloads, never counted a second time or page.
                if item.get('trace_id'):continue
                if item.get('tool_name'):calls.append({**item,'layer':'specialist','legacy':True,'event_seq':event['seq']})
    trace={'turn_key':current.get('turn_key'),'status':current['status'],'event_cursor':current['event_cursor'],
        'event_end':current['event_end'],'snapshot_end':end,'latency_ms':current.get('latency_ms'),
        'timing_epoch':current.get('timing_epoch'),'timing_source':current.get('timing_source'),
        'tool_calls':calls,'tools_used':list(dict.fromkeys(c['tool_name'] for c in calls if c.get('tool_name'))),
        'fallback_events':fallbacks,'events':events,'next_after':events[-1]['seq'] if more else None}
    return {'request_id':run_id,'found':True,'trace':trace}


def recent_traces(store,p,*,before=None,limit=20):
    params=[p.tenant,p.user]
    where='tenant=? AND owner=?'
    if before is not None:where+=' AND user_seq<?';params.append(before)
    with store.db() as db:
        rows=db.execute('SELECT run_id,request_key,user_seq,status,response,updated FROM turn_receipts WHERE '+where+' ORDER BY user_seq DESC LIMIT ?',[*params,limit+1]).fetchall()
    items=[]
    for row in rows[:limit]:
        saved=json.loads(row['response']) if row['response'] else {}
        items.append({'request_id':row['run_id'],'turn_key':row['request_key'],'turn_seq':row['user_seq'],
            'status':row['status'],'updated':row['updated'],'trace_available':row['response'] is not None,
            'latency_ms':saved.get('latency_ms'),'timing_epoch':saved.get('timing_epoch')})
    return {'items':items,'next_before':items[-1]['turn_seq'] if len(rows)>limit else None,'scope':'own_turns'}
