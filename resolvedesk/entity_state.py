"""Per-conversation IT entities with literal provenance and explicit task resets."""
import json
import re

from resolvedesk.entities import extract
from resolvedesk.store import encode


class EntityState:
    def __init__(self,store):
        self.store=store
        with store.database.schema_transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS entity_state(run_id TEXT PRIMARY KEY,tenant TEXT,owner TEXT,
                turn_seq INTEGER,domain TEXT,payload TEXT,updated REAL)""")

    def get(self,p,run_id):
        self.store.get_run(p,run_id)
        with self.store.db() as db:
            row=db.execute("SELECT * FROM entity_state WHERE run_id=? AND tenant=? AND owner=?",(run_id,p.tenant,p.user)).fetchone()
            return json.loads(row["payload"]) if row else {"domain":"general","entities":{},"sources":{},"task_epoch":0}

    def merge(self,p,run_id,message,domain):
        extraction=extract(message);entities=extraction['values']
        with self.store.db(True) as db:
            self.store._run(db,p,run_id)
            seq=db.execute("SELECT MAX(seq) FROM messages WHERE run_id=? AND role='user'",(run_id,)).fetchone()[0]
            row=db.execute(db.locked("SELECT * FROM entity_state WHERE run_id=? AND tenant=? AND owner=?"),(run_id,p.tenant,p.user)).fetchone()
            previous=json.loads(row["payload"]) if row else {"domain":"general","entities":{},"sources":{},"task_epoch":0}
            if row and row["turn_seq"]==seq:return previous
            old=previous["entities"]
            cancelled=bool(re.search(r'^(?:请|麻烦|我想|我要|请帮我)?(?:取消(?:之前|原来|刚才|本次|这个|全部)?(?:的)?(?:权限)?(?:申请|任务|问题)|先不处理)|^之前的全部取消',message))
            switches=list(re.finditer(r"(?:换个|新的?|另一个|另外一个)(?:问题|任务|设备|申请)",message))
            switched=any(not re.search(r'(?:不|不要|别|无需)$',message[max(0,match.start()-4):match.start()]) for match in switches)
            replaced=any(entities.get(key) and old.get(key) and entities[key]!=old[key] for key in ("asset_id","resource_id","ticket_id"))
            changed_domain=domain not in {"general","escalation"} and previous["domain"] not in {domain,"general","escalation"}
            reset=switched or replaced or changed_domain or cancelled
            merged={} if reset else dict(old)
            sources={} if reset else dict(previous["sources"])
            for key in extraction['cleared']:
                merged.pop(key,None);sources.pop(key,None)
            for key,values in entities.items():
                merged[key]=values
                sources[key]={"turn_seq":seq,"source":"explicit_user_text","values":values,'spans':extraction['spans'][key]}
            if cancelled and not switched and not changed_domain and not entities:domain='general'
            if domain in {"general","escalation"} and not reset:domain=previous["domain"]
            from resolvedesk.task_contract import update
            task=update(previous.get('task'),message,domain,seq,reset=reset,cancelled=cancelled and not entities and not switched and not changed_domain)
            payload={"domain":domain,"entities":merged,"sources":sources,"task":task,"task_epoch":previous["task_epoch"]+int(reset),"reset":reset,"turn_seq":seq,
                'cleared_fields':extraction['cleared'],'cancelled':cancelled and not entities and not switched and not changed_domain}
            db.execute("INSERT INTO entity_state VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET turn_seq=excluded.turn_seq,domain=excluded.domain,payload=excluded.payload,updated=excluded.updated",
                (run_id,p.tenant,p.user,seq,domain,encode(payload),db.now()))
            return payload
