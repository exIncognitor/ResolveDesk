"""Frozen authorized knowledge and synthetic business data in isolated real services."""
import hashlib
import uuid

from resolvedesk.store import Store,digest,encode


def freeze(state,p,dataset,root):
    def prepare(db):
        rows=[dict(r) for r in db.execute("SELECT * FROM chunks WHERE tenant=? AND visibility='tenant' ORDER BY id",(p.tenant,))]
        identity='eval_rd_'+uuid.uuid4().hex
        sources={}
        for folder in ('resolvedesk','agents','core','tooling'):
            for path in sorted((root/folder).glob('*.py')):
                sources[str(path.relative_to(root))]=hashlib.sha256(path.read_bytes()).hexdigest()
        return {'dataset':dataset,'evidence':rows,'identity':identity,'backend':state.store.backend,
                'engine_sources':sources,'source_identity':digest([str(state.store.database.engine.url),state.store.database.schema])}
    return state.snapshot(prepare)


class EvalSandbox:
    def __init__(self,source,knowledge,manifest,scratch,coordination=None):
        self.manifest,self.source=manifest,source
        self.store=self.kb=self.coordination=self.vectors=None
        self.scratch,self.original_kb,self.original_coordination=scratch,knowledge,coordination

    async def open(self,p):
        from resolvedesk.retrieval import HybridKnowledge
        m=self.manifest
        if m['backend']!=self.source.backend:raise ValueError('Sandbox backend changed')
        if self.source.backend=='postgresql':
            if not self.original_kb.vector_index or not self.original_coordination:raise ValueError('Formal quality evaluation requires PostgreSQL, Redis and Chroma')
            from resolvedesk.coordination import Coordination
            from resolvedesk.vectors import ChromaVectors
            self.store=Store(self.source.database.engine.url.render_as_string(hide_password=False),schema=m['identity'])
            # Never use the production collection or Redis namespace for evaluation.
            self.vectors=ChromaVectors(str(self.original_kb.vector_index.http.base_url),self.original_kb.embedding.identity,collection=m['identity'].replace('_','-'))
            self.coordination=Coordination(self.original_coordination.redis.connection_pool.connection_kwargs.get('url','redis://localhost')) if False else None
            from redis.asyncio import Redis
            self.coordination=object.__new__(Coordination)
            self.coordination.namespace=self.original_coordination.namespace+':'+m['identity']
            self.coordination.redis=Redis(connection_pool=self.original_coordination.redis.connection_pool)
            await self.coordination.redis.ping()
        else:
            self.scratch.mkdir(parents=True,exist_ok=True)
            self.store=Store(self.scratch/'sandbox.sqlite3')
        self.kb=HybridKnowledge(self.store,self.original_kb.embedding,self.vectors)
        # A separate initialization marker makes reopening idempotent without
        # overwriting a progressed workflow or an earlier frozen knowledge copy.
        with self.store.db(True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS eval_sandbox_meta(id INTEGER PRIMARY KEY,fingerprint TEXT)')
            row=db.execute('SELECT fingerprint FROM eval_sandbox_meta WHERE id=1').fetchone()
            expected=digest(m)
            if row:
                if row[0]!=expected:raise ValueError('Sandbox snapshot mismatch')
            else:
                for row in m['evidence']:
                    columns=list(row)
                    db.execute('INSERT INTO chunks('+','.join('"'+c+'"' for c in columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(row[c] for c in columns))
                for doc in sorted({r['document_id'] for r in m['evidence']}):
                    item=next(r for r in m['evidence'] if r['document_id']==doc)
                    db.execute('INSERT INTO knowledge_documents VALUES(?,?,?,?,?,1,?)',(p.tenant,doc,item['owner'],item['visibility'],item['version'],db.now()))
                    if self.vectors:db.execute("INSERT INTO index_outbox VALUES(?,?,?,1,'pending',0,?,NULL,?)",(uuid.uuid4().hex,p.tenant,doc,db.now(),db.now()))
                for asset in m['dataset']['assets']:
                    db.execute("INSERT INTO desk_assets VALUES(?,?,?,?,?,'available')",(p.tenant,asset['id'],asset['owner'],asset['model'],asset['warranty']))
                db.execute('INSERT INTO eval_sandbox_meta VALUES(1,?)',(expected,))
        if self.vectors:
            result=self.kb.drain_index(1000)
            if result['failed'] or result['pending']:raise ValueError('Frozen evidence Chroma projection is incomplete')
        return self

    async def close(self):
        if self.coordination:await self.coordination.close()
        if self.vectors:self.vectors.close()
        if self.store:self.store.close()
