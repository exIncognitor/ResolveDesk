"""Self-hosted CPU E5 endpoint; no paid API, no implicit model download."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import os
from pathlib import Path

from fastapi import FastAPI,Header,HTTPException
from pydantic import BaseModel,ConfigDict,Field

from resolvedesk.retrieval import LocalEmbedding

ROOT=Path(__file__).resolve().parents[1]


class EmbeddingInput(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    model:str
    input:list[str]=Field(min_length=1,max_length=32)
    query:bool=False


@asynccontextmanager
async def lifespan(app):
    if len(os.getenv("RESOLVEDESK_EMBEDDING_TOKEN",""))<32:raise RuntimeError("Configure a dedicated embedding service token")
    app.state.embedding=LocalEmbedding(ROOT/"data/models/multilingual-e5-small")
    app.state.slots=asyncio.Semaphore(2)
    yield


app=FastAPI(title="ResolveDesk pinned E5 embeddings",lifespan=lifespan,docs_url=None,redoc_url=None)


@app.get("/health")
def health():return {"ready":True,"model":app.state.embedding.identity,"paid_api":False}


@app.post("/v1/embeddings")
async def embeddings(data:EmbeddingInput,authorization:str=Header(default="")):
    token=os.getenv("RESOLVEDESK_EMBEDDING_TOKEN","")
    if not token or not hmac.compare_digest(authorization,"Bearer "+token):raise HTTPException(401,"Embedding service token required")
    if data.model!=app.state.embedding.identity:raise HTTPException(422,"Pinned embedding identity required")
    if any(not text.strip() or len(text)>4000 for text in data.input) or sum(len(t.encode()) for t in data.input)>128000:
        raise HTTPException(422,"Embedding input limit exceeded")
    async with app.state.slots:
        vectors=await asyncio.to_thread(app.state.embedding.embed,data.input,data.query)
    return {"model":app.state.embedding.identity,"data":[{"index":i,"embedding":v} for i,v in enumerate(vectors)]}
