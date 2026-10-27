"""Bounded Chroma v2 HTTP protocol adapter with explicit local embeddings.

The pinned SDK's HTTP transport sets timeout=None even during initialization.
This adapter uses the same documented v2 operations with hard HTTP deadlines,
no implicit embedding download, and no automatic write retry. SQL outbox owns
idempotent retries. Chroma remains the real HNSW service, not a local substitute.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

import httpx


class ChromaVectors:
    def __init__(self,url,identity,*,collection=None):
        parts = urlsplit(url)
        if parts.scheme not in {"http","https"} or parts.username or parts.password or parts.path not in {"","/"}:
            raise ValueError("Invalid Chroma service URL")
        self.http = httpx.Client(base_url=url.rstrip("/"),timeout=httpx.Timeout(5,connect=2),trust_env=False)
        self.identity = identity
        self.name = collection or "resolvedesk-kb-"+hashlib.sha256(identity.encode()).hexdigest()[:16]
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,62}",self.name): raise ValueError("Invalid collection name")
        self.base = "/api/v2/tenants/default_tenant/databases/default_database/collections"
        result = self.request("POST",self.base,json={"name":self.name,"get_or_create":True,
            "metadata":{"embedding_identity":identity},"configuration":{"hnsw":{"space":"cosine"}}})
        if result["metadata"].get("embedding_identity") != identity: raise ValueError("Collection embedding identity mismatch")
        self.path = self.base+"/"+result["id"]

    def request(self,method,path,**kwargs):
        result = self.http.request(method,path,**kwargs)
        result.raise_for_status()
        return result.json() if result.content else None

    @staticmethod
    def scope(p):
        return {"$and":[{"tenant":{"$eq":p.tenant}},
            {"$or":[{"visibility":{"$eq":"tenant"}},{"owner":{"$eq":p.user}}]}]}

    def replace(self,tenant,document_id,rows):
        self.request("POST",self.path+"/delete",json={"where":{"$and":[{"tenant":{"$eq":tenant}},{"document_id":{"$eq":document_id}}]}})
        if rows:
            # Bounded requests, explicit vectors: server never downloads an embedding model.
            for start in range(0,len(rows),64):
                batch = rows[start:start+64]
                self.request("POST",self.path+"/upsert",json={"ids":[r["id"] for r in batch],
                    "embeddings":[r["vector"] for r in batch],"metadatas":[{k:r[k] for k in ("tenant","owner","visibility","document_id","version")} for r in batch]})

    def search(self,p,vector,count=40,*,document_id=None,exclude_document=None,private_only=False):
        clauses=[self.scope(p)]
        if private_only:
            clauses += [{"owner":{"$eq":p.user}},{"visibility":{"$eq":"private"}}]
        if document_id is not None:clauses.append({"document_id":{"$eq":document_id}})
        if exclude_document is not None:clauses.append({"document_id":{"$ne":exclude_document}})
        result = self.request("POST",self.path+"/query",json={"query_embeddings":[vector],"n_results":count,
            "where":clauses[0] if len(clauses)==1 else {"$and":clauses},"include":["distances","metadatas"]})
        return [{"id":key,"cosine":1-distance,"metadata":meta} for key,distance,meta in
            zip(result["ids"][0],result["distances"][0],result["metadatas"][0],strict=True)]

    def count(self): return self.request("GET",self.path+"/count")
    def close(self): self.http.close()
