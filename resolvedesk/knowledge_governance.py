"""SQL publication authority. Source URLs are data and are never fetched here."""
import json
import uuid
from datetime import date
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator

from resolvedesk.store import Conflict,encode

GOVERNANCE_TABLES={'knowledge_publications','knowledge_publication_audit'}


class KnowledgeMetadata(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    material_type:Literal['technical_reference','simulated_policy']
    source_urls:list[str]=Field(max_length=12)
    applicability:list[str]=Field(min_length=1,max_length=20)
    domains:list[Literal['account_access','network_endpoint','software_license','device_service']]=Field(min_length=1,max_length=4)
    source_checked_on:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    effective_from:float=Field(ge=0,le=4102444800)
    expires_at:float=Field(gt=0,le=4102444800)

    @field_validator('source_urls')
    @classmethod
    def sources(cls,values):
        for value in values:
            parts=urlsplit(value)
            if len(value)>2048 or parts.scheme!='https' or not parts.hostname or parts.username or parts.password or any(c.isspace() for c in value):
                raise ValueError('Sources require bounded HTTPS URLs without credentials')
        return values

    @field_validator('applicability')
    @classmethod
    def systems(cls,values):
        if any(not value.strip() or len(value)>200 for value in values):raise ValueError('Invalid applicability')
        return values

    @field_validator('source_checked_on')
    @classmethod
    def checked_date(cls,value):
        date.fromisoformat(value)
        return value

    @model_validator(mode='after')
    def consistent(self):
        if self.expires_at<=self.effective_from:raise ValueError('Expiry must follow effective time')
        if self.material_type=='technical_reference' and not self.source_urls:raise ValueError('Official source required')
        return self


def clock_sql(db):
    return 'EXTRACT(EPOCH FROM clock_timestamp())' if db.backend=='postgresql' else "((julianday('now')-2440587.5)*86400.0)"


def readable(db,alias='c'):
    now=clock_sql(db)
    return f"""(NOT EXISTS(SELECT 1 FROM knowledge_publications kp WHERE kp.tenant={alias}.tenant AND kp.document_id={alias}.document_id)
        OR EXISTS(SELECT 1 FROM knowledge_publications kp WHERE kp.tenant={alias}.tenant AND kp.document_id={alias}.document_id
          AND kp.state='published' AND kp.content_version={alias}.version AND kp.effective_from<={now} AND kp.expires_at>{now}))"""


class KnowledgeGovernance:
    def __init__(self,store):
        self.store=store
        with store.database.schema_transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS knowledge_publications(tenant TEXT,document_id TEXT,metadata TEXT NOT NULL,
                    revision INTEGER NOT NULL,content_version TEXT NOT NULL,state TEXT NOT NULL,reviewer TEXT,reviewed REAL,
                    note TEXT,effective_from REAL NOT NULL,expires_at REAL NOT NULL,updated REAL NOT NULL,
                    PRIMARY KEY(tenant,document_id));
                CREATE TABLE IF NOT EXISTS knowledge_publication_audit(id TEXT PRIMARY KEY,tenant TEXT,document_id TEXT,
                    actor TEXT,action TEXT,revision INTEGER,content_version TEXT,note TEXT,created REAL);
                CREATE INDEX IF NOT EXISTS publication_audit_scope ON knowledge_publication_audit(tenant,document_id,created);
            ''')

    @staticmethod
    def owner(db,p,document_id):
        if p.role!='support':raise PermissionError('Support role required for knowledge review')
        doc=db.execute(db.locked('SELECT * FROM knowledge_documents WHERE tenant=? AND document_id=?'),(p.tenant,document_id)).fetchone()
        if doc is None or (doc['visibility']=='private' and doc['owner']!=p.user):raise KeyError('Document unavailable')
        return doc

    @staticmethod
    def audit(db,p,document_id,action,revision,version,note):
        db.execute('INSERT INTO knowledge_publication_audit VALUES(?,?,?,?,?,?,?,?,?)',
            (uuid.uuid4().hex,p.tenant,document_id,p.user,action,revision,version,note,db.now()))

    def import_metadata(self,db,p,document_id,version,metadata):
        old=db.execute('SELECT * FROM knowledge_publications WHERE tenant=? AND document_id=?',(p.tenant,document_id)).fetchone()
        if metadata is None and old is None:return
        value=KnowledgeMetadata.model_validate(metadata if metadata is not None else json.loads(old['metadata'])).model_dump()
        revision=old['revision']+1 if old else 1
        db.execute('''INSERT INTO knowledge_publications VALUES(?,?,?,?,?,'draft',NULL,NULL,?,?,?,?)
            ON CONFLICT(tenant,document_id) DO UPDATE SET metadata=excluded.metadata,revision=excluded.revision,
            content_version=excluded.content_version,state='draft',reviewer=NULL,reviewed=NULL,note=excluded.note,
            effective_from=excluded.effective_from,expires_at=excluded.expires_at,updated=excluded.updated''',
            (p.tenant,document_id,encode(value),revision,version,'Content imported; review required',value['effective_from'],value['expires_at'],db.now()))
        self.audit(db,p,document_id,'import_draft',revision,version,'Content imported; review required')

    @staticmethod
    def enqueue(db,p,doc,generation,metadata=None):
        now=db.now()
        # Superseded future jobs must not hold a new revision in degraded mode.
        db.execute("UPDATE index_outbox SET state='done' WHERE tenant=? AND document_id=? AND state='pending'",(p.tenant,doc))
        times=[now]+([t for t in (metadata['effective_from'],metadata['expires_at']) if t>now] if metadata else [])
        for due in sorted(set(times)):
            db.execute("INSERT INTO index_outbox VALUES(?,?,?,?,'pending',0,?,NULL,?)",(uuid.uuid4().hex,p.tenant,doc,generation,due,now))

    def change(self,p,document_id,action,version,revision,note,metadata=None):
        if action not in {'publish','withdraw','metadata'}:raise ValueError('Unknown publication action')
        if not isinstance(note,str) or not note.strip() or len(note)>2000:raise ValueError('Review note required')
        if type(revision)is not int or revision<1:raise ValueError('Metadata revision required')
        value=KnowledgeMetadata.model_validate(metadata).model_dump() if action=='metadata' else None
        with self.store.db(True) as db:
            doc=self.owner(db,p,document_id)
            row=db.execute('SELECT * FROM knowledge_publications WHERE tenant=? AND document_id=?',(p.tenant,document_id)).fetchone()
            if row is None:raise KeyError('Managed publication unavailable')
            if doc['version']!=version or row['content_version']!=version or row['revision']!=revision:raise Conflict('Content or metadata changed; refresh before reviewing')
            value=value or json.loads(row['metadata']);now=db.now()
            if action=='publish' and value['expires_at']<=now:raise Conflict('Expired material cannot be published')
            state={'publish':'published','withdraw':'withdrawn','metadata':'draft'}[action]
            db.execute('''UPDATE knowledge_publications SET metadata=?,revision=?,state=?,reviewer=?,reviewed=?,note=?,
                effective_from=?,expires_at=?,updated=? WHERE tenant=? AND document_id=?''',
                (encode(value),revision+1,state,p.user if action!='metadata' else None,now if action!='metadata' else None,note,
                 value['effective_from'],value['expires_at'],now,p.tenant,document_id))
            generation=doc['generation']+1
            db.execute('UPDATE knowledge_documents SET generation=?,updated=? WHERE tenant=? AND document_id=?',(generation,now,p.tenant,document_id))
            self.audit(db,p,document_id,action,revision+1,version,note)
            self.enqueue(db,p,document_id,generation,value if state=='published' else None)
        return next(d for d in self.list(p) if d['document_id']==document_id)

    def list(self,p):
        if p.role!='support':raise PermissionError('Support role required for knowledge review')
        with self.store.db() as db:
            now=db.now()
            rows=db.execute('''SELECT k.*,d.version,d.visibility,d.owner,
                (SELECT MIN(c.title) FROM chunks c WHERE c.tenant=d.tenant AND c.document_id=d.document_id) AS title
                FROM knowledge_publications k JOIN knowledge_documents d ON d.tenant=k.tenant AND d.document_id=k.document_id
                WHERE d.tenant=? AND (d.visibility='tenant' OR d.owner=?) ORDER BY d.document_id LIMIT 1000''',(p.tenant,p.user))
            return [dict(r)|{'metadata':json.loads(r['metadata']),'readable':r['state']=='published' and r['effective_from']<=now<r['expires_at'] and r['version']==r['content_version']} for r in rows]

    def audit_list(self,p,document_id):
        with self.store.db() as db:
            self.owner(db,p,document_id)
            return [dict(r) for r in db.execute('SELECT * FROM knowledge_publication_audit WHERE tenant=? AND document_id=? ORDER BY created,id',(p.tenant,document_id))]
