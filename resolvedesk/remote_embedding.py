"""Explicit bounded client for the self-hosted pinned E5 embedding service.

It never calls an unknown paid provider or reads legacy third-party keys.
Template and query vectors always use this same configured provider/model.
"""
import math
from urllib.parse import urlsplit

import httpx

from resolvedesk.retrieval import LocalEmbedding


class HTTPEmbedding:
    def __init__(self,url,token,*,transport=None):
        parsed=urlsplit(url)
        if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Invalid embedding service URL")
        if len(token)<32:raise ValueError("Self-hosted embedding token must be configured")
        self.model=LocalEmbedding.model_id+"@"+LocalEmbedding.revision
        self.identity="self-hosted-http/"+self.model
        self.http=httpx.Client(base_url=url.rstrip("/"),headers={"Authorization":"Bearer "+token},
            timeout=httpx.Timeout(6,connect=2),trust_env=False,follow_redirects=False,transport=transport)

    def embed(self,texts,query=False):
        if not 1<=len(texts)<=32:raise ValueError("Embedding batch must contain 1-32 texts")
        result=self.http.post("/v1/embeddings",json={"model":self.model,"input":texts,"query":bool(query)})
        result.raise_for_status();payload=result.json()
        if payload.get("model")!=self.model:raise ValueError("Remote embedding model identity mismatch")
        data=payload.get("data")
        if not isinstance(data,list) or len(data)!=len(texts):raise ValueError("Remote embedding count mismatch")
        rows={};dimension=None
        for row in data:
            if not isinstance(row,dict) or type(row.get("index"))is not int or not 0<=row["index"]<len(texts) or row["index"] in rows:
                raise ValueError("Invalid remote embedding index")
            vector=row.get("embedding")
            if not isinstance(vector,list) or not vector or any(type(v)not in {int,float} or not math.isfinite(v) for v in vector):raise ValueError("Invalid remote embedding vector")
            norm=math.sqrt(sum(v*v for v in vector))
            dimension=dimension or len(vector)
            if not norm or not math.isfinite(norm) or len(vector)!=dimension:raise ValueError("Invalid remote embedding dimensions/norm")
            rows[row["index"]]=[v/norm for v in vector]
        return [rows[i] for i in range(len(texts))]

    def close(self):self.http.close()
