"""SQL publication authority. Source URLs are data and are never fetched here."""
import json
import uuid
from datetime import date
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator

from resolvedesk.store import Conflict,encode

GOVERNANCE_TABLES={'knowledge_publications','knowledge_publication_audit','knowledge_legacy_documents','knowledge_governance_migrations'}


def pending_metadata():
    return {'material_type':'unclassified','source_urls':[],'applicability':[],'domains':[],
            'source_checked_on':'','effective_from':0,'expires_at':0}


def metadata_complete(value):
    try:KnowledgeMetadata.model_validate(value);return True
    except ValueError:return False


def publication_identity(publications):
    """Semantic inputs, excluding audit actor/time and review counters."""
    return sorted(({key:r.get(key) for key in ('tenant','document_id','content_version','state','metadata')} for r in publications),
                  key=lambda r:(r.get('tenant') or '',r['document_id']))


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
    return f"""((NOT EXISTS(SELECT 1 FROM knowledge_publications kp WHERE kp.tenant={alias}.tenant AND kp.document_id={alias}.document_id)
        AND EXISTS(SELECT 1 FROM knowledge_legacy_documents kl WHERE kl.tenant={alias}.tenant AND kl.document_id={alias}.document_id AND kl.version={alias}.version))
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
                CREATE TABLE IF NOT EXISTS knowledge_legacy_documents(tenant TEXT,document_id TEXT,version TEXT,PRIMARY KEY(tenant,document_id));
                CREATE TABLE IF NOT EXISTS knowledge_governance_migrations(id TEXT PRIMARY KEY,applied REAL);
            ''')
            if not db.execute("SELECT 1 FROM knowledge_governance_migrations WHERE id='managed-import-v1'").fetchone():
                db.execute('''INSERT INTO knowledge_legacy_documents SELECT d.tenant,d.document_id,d.version FROM knowledge_documents d
                    WHERE NOT EXISTS(SELECT 1 FROM knowledge_publications k WHERE k.tenant=d.tenant AND k.document_id=d.document_id)
                    ON CONFLICT(tenant,document_id) DO NOTHING''')
                db.execute("INSERT INTO knowledge_governance_migrations VALUES('managed-import-v1',?)",(db.now(),))

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
        value=KnowledgeMetadata.model_validate(metadata).model_dump() if metadata is not None else json.loads(old['metadata']) if old else pending_metadata()
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
            legacy=bool(db.execute('SELECT 1 FROM knowledge_legacy_documents WHERE tenant=? AND document_id=? AND version=?',(p.tenant,document_id,doc['version'])).fetchone())
            if row is None and not (action=='metadata' and revision==0 and legacy):raise KeyError('Managed publication unavailable')
            if doc['version']!=version or (row and (row['content_version']!=version or row['revision']!=revision)):raise Conflict('Content or metadata changed; refresh before reviewing')
            value=value or json.loads(row['metadata']);now=db.now()
            if action=='publish':
                value=KnowledgeMetadata.model_validate(value).model_dump()
                if value['expires_at']<=now:raise Conflict('Expired material cannot be published')
            state={'publish':'published','withdraw':'withdrawn','metadata':'draft'}[action]
            db.execute('''INSERT INTO knowledge_publications VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant,document_id) DO UPDATE SET metadata=excluded.metadata,revision=excluded.revision,
                state=excluded.state,reviewer=excluded.reviewer,reviewed=excluded.reviewed,note=excluded.note,
                effective_from=excluded.effective_from,expires_at=excluded.expires_at,updated=excluded.updated''',
                (p.tenant,document_id,encode(value),revision+1,version,state,p.user if action!='metadata' else None,
                 now if action!='metadata' else None,note,value['effective_from'],value['expires_at'],now))
            generation=doc['generation']+1
            db.execute('UPDATE knowledge_documents SET generation=?,updated=? WHERE tenant=? AND document_id=?',(generation,now,p.tenant,document_id))
            self.audit(db,p,document_id,action,revision+1,version,note)
            self.enqueue(db,p,document_id,generation,value if state=='published' else None)
        return self.list(p,document_id=document_id)[0]

    def list(self,p,*,document_id=None):
        if p.role!='support':raise PermissionError('Support role required for knowledge review')
        with self.store.db() as db:
            now=db.now()
            extra=' AND d.document_id=?' if document_id is not None else ''
            rows=db.execute('''SELECT d.*,k.metadata,k.revision,k.content_version,k.state,k.reviewer,k.reviewed,k.note,k.effective_from,k.expires_at,
                (SELECT MIN(c.title) FROM chunks c WHERE c.tenant=d.tenant AND c.document_id=d.document_id) AS title
                FROM knowledge_documents d LEFT JOIN knowledge_publications k ON d.tenant=k.tenant AND d.document_id=k.document_id
                WHERE d.tenant=? AND (d.visibility='tenant' OR d.owner=?)'''+extra+' ORDER BY d.document_id LIMIT 1000',[p.tenant,p.user]+([document_id] if document_id is not None else []))
            result=[]
            for row in rows:
                item=dict(row);value=json.loads(item['metadata']) if item['metadata'] else pending_metadata()
                legacy=bool(db.execute('SELECT 1 FROM knowledge_legacy_documents WHERE tenant=? AND document_id=? AND version=?',(p.tenant,item['document_id'],item['version'])).fetchone())
                state=item['state'] or ('legacy' if legacy else 'draft')
                result.append(item|{'metadata':value,'metadata_complete':metadata_complete(value),'state':state,'revision':item['revision'] or 0,
                    'content_version':item['content_version'] or item['version'],'readable':state=='legacy' or state=='published' and item['effective_from']<=now<item['expires_at'] and item['version']==item['content_version']})
            return result

    def audit_list(self,p,document_id):
        with self.store.db() as db:
            self.owner(db,p,document_id)
            return [dict(r) for r in db.execute('SELECT * FROM knowledge_publication_audit WHERE tenant=? AND document_id=? ORDER BY created,id',(p.tenant,document_id))]
