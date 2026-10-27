"""Scoped hybrid retrieval with exact local vectors, BM25 and rank fusion.

SQLite is the source of truth for chunks and vectors. Both retrieval branches
receive ONLY documents authorized for the current principal. Exact cosine is
intentional for this local corpus; use a filtered vector service at larger scale.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re

from resolvedesk.store import encode


class LocalEmbedding:
    model_id = "Xenova/multilingual-e5-small"
    revision = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"

    def __init__(self, model_dir):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        model_dir = Path(model_dir)
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        self.session = ort.InferenceSession(str(model_dir / "model_quantized.onnx"), options,
                                           providers=["CPUExecutionProvider"])
        self.identity = self.model_id + "@" + self.revision

    def embed(self, texts, query=False):
        import numpy as np
        result = []
        for start in range(0, len(texts), 8):
            batch = self.tokenizer.encode_batch([("query: " if query else "passage: ") + t for t in texts[start:start+8]])
            ids = np.asarray([e.ids for e in batch], dtype=np.int64)
            mask = np.asarray([e.attention_mask for e in batch], dtype=np.int64)
            available = {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)}
            outputs = self.session.run(None, {i.name: available[i.name] for i in self.session.get_inputs()})
            hidden = outputs[0]
            if hidden.ndim != 3:
                raise ValueError("Unexpected embedding model output")
            pooled = (hidden * mask[:, :, None]).sum(axis=1) / np.maximum(mask.sum(axis=1)[:, None], 1)
            pooled /= np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12)
            result.extend(pooled.tolist())
        return result


def download_embedding(model_dir):
    """Explicit setup operation; serving never silently downloads a model."""
    from huggingface_hub import hf_hub_download
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    for name in ("tokenizer.json", "onnx/model_quantized.onnx"):
        target = model_dir / Path(name).name
        if target.exists():
            continue
        cached = hf_hub_download(LocalEmbedding.model_id, name, revision=LocalEmbedding.revision)
        # Only initial creation, never overwrite a local model file.
        with Path(cached).open("rb") as source, target.open("xb") as dest:
            import shutil
            shutil.copyfileobj(source, dest)


def markdown_chunks(text, size=700):
    """Contiguous source spans, heading paths, paragraph/fence-aware boundaries."""
    blocks, headings, offset, start, section, in_code = [], [], 0, 0, "", False
    for line in text.splitlines(keepends=True):
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line) if not in_code else None
        if heading:
            if offset > start:
                blocks.append((start, offset, section))
            level = len(heading[1])
            headings = [(n, h) for n, h in headings if n < level] + [(level, heading[2])]
            section, start = " / ".join(h for _, h in headings), offset
        if line.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
        offset += len(line)
        if not line.strip() and not in_code and offset-start >= size//2:
            blocks.append((start, offset, section))
            start = offset
    if offset > start:
        blocks.append((start, offset, section))
    chunks = []
    for begin, end, section in blocks:
        while begin < end:
            stop = min(begin+size, end)
            if stop < end:
                boundary = text.rfind("\n", begin+size//2, stop)
                if boundary >= 0:
                    stop = boundary+1
            if text[begin:stop].strip():
                chunks.append({"content": text[begin:stop], "start": begin, "end": stop, "section": section})
            begin = stop
    return chunks


def terms(text):
    text = text.lower()
    words = re.findall(r"[a-z0-9_]+(?:[-.][a-z0-9_]+)*", text)
    for group in re.findall(r"[\u3400-\u9fff]+", text):
        words.extend(group)
        words.extend(group[i:i+2] for i in range(len(group)-1))
    return words


def bm25(query, documents):
    tokens = [Counter(terms(d)) for d in documents]
    if not tokens:
        return []
    avg = sum(sum(t.values()) for t in tokens) / len(tokens) or 1
    scores = [0.0]*len(tokens)
    for term in set(terms(query)):
        freq = sum(term in row for row in tokens)
        idf = math.log(1 + (len(tokens)-freq+0.5)/(freq+0.5))
        for i, row in enumerate(tokens):
            tf = row[term]
            if tf:
                scores[i] += idf*tf*2.2/(tf+1.2*(0.25+0.75*sum(row.values())/avg))
    return scores


def rrf(rankings, k=60):
    scores = Counter()
    for ranking in rankings:
        for rank, item in enumerate(dict.fromkeys(ranking), 1):
            scores[item] += 1/(k+rank)
    return sorted(scores, key=lambda i: (-scores[i], i)), scores


class HybridKnowledge:
    def __init__(self, store, embedding=None):
        self.store, self.embedding = store, embedding
        with store.database.schema_transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS chunks(
                id TEXT PRIMARY KEY, tenant TEXT NOT NULL, owner TEXT NOT NULL,
                visibility TEXT NOT NULL, document_id TEXT NOT NULL, title TEXT NOT NULL,
                version TEXT NOT NULL, section TEXT NOT NULL, start INTEGER NOT NULL,
                "end" INTEGER NOT NULL, content TEXT NOT NULL, vector TEXT, embedding TEXT,
                UNIQUE(tenant,document_id,version,start))""")
            db.execute("CREATE INDEX IF NOT EXISTS chunk_scope ON chunks(tenant,document_id)")

    def add(self, p, document_id, title, content, visibility="tenant"):
        if p.role != "support":
            raise PermissionError("Support role required to import documents")
        if visibility not in {"tenant", "private"} or not all(isinstance(v, str) and v.strip() for v in (document_id, title, content)):
            raise ValueError("Invalid document")
        if len(content) > 100_000:
            raise ValueError("Document exceeds 100000 characters")
        chunks = markdown_chunks(content)
        version = hashlib.sha256(content.encode()).hexdigest()[:16]
        vectors = self.embedding.embed([title+"\n"+c["section"]+"\n"+c["content"] for c in chunks]) if self.embedding else [None]*len(chunks)
        with self.store.db(True) as db:
            existing = db.execute("SELECT owner,visibility FROM chunks WHERE tenant=? AND document_id=? LIMIT 1", (p.tenant, document_id)).fetchone()
            if existing and existing["visibility"] == "private" and existing["owner"] != p.user:
                raise PermissionError("A private document can only be replaced by its owner")
            db.execute("DELETE FROM chunks WHERE tenant=? AND document_id=?", (p.tenant, document_id))
            for chunk, vector in zip(chunks, vectors, strict=True):
                cid = hashlib.sha256(encode([p.tenant, document_id, version, chunk["start"]]).encode()).hexdigest()[:24]
                db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (cid, p.tenant, p.user, visibility, document_id, title, version,
                            chunk["section"], chunk["start"], chunk["end"], chunk["content"],
                            encode(vector) if vector is not None else None,
                            self.embedding.identity if vector is not None else None))
        return {"document_id": document_id, "version": version, "chunks": len(chunks)}

    def search(self, p, query, top_k=5, mode="hybrid"):
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("Query must contain 1-2000 characters")
        if type(top_k) is not int or not 1 <= top_k <= 20 or mode not in {"hybrid", "bm25", "vector"}:
            raise ValueError("Invalid retrieval options")
        with self.store.db() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM chunks WHERE tenant=? AND (visibility='tenant' OR owner=?) ORDER BY id",
                                                   (p.tenant, p.user))]
        if not rows:
            return {"items": [], "mode": mode, "degraded": False, "candidate_count": 0}
        lexical = bm25(query, [r["title"]+"\n"+r["section"]+"\n"+r["content"] for r in rows])
        lexical_rank = sorted((i for i in range(len(rows)) if lexical[i] > 0), key=lambda i: (-lexical[i], rows[i]["id"]))[:40]
        vector_rank, semantic, degraded = [], {}, False
        if mode != "bm25":
            if self.embedding is None:
                degraded = True
            else:
                vector = self.embedding.embed([query], query=True)[0]
                for i, row in enumerate(rows):
                    if row["embedding"] == self.embedding.identity and row["vector"]:
                        stored = json.loads(row["vector"])
                        if len(stored) != len(vector):
                            raise ValueError("Embedding dimensions differ; reimport document")
                        semantic[i] = sum(a*b for a,b in zip(stored, vector))
                degraded = len(semantic) != len(rows)
                vector_rank = sorted(semantic, key=lambda i: (-semantic[i], rows[i]["id"]))[:40]
        rankings = [lexical_rank] if mode == "bm25" else [vector_rank] if mode == "vector" else [lexical_rank, vector_rank]
        order, scores = rrf(rankings)
        items = []
        for i in order[:top_k]:
            row = rows[i]
            items.append({k: row[k] for k in ("document_id", "title", "version", "section", "start", "end", "content")} | {
                "citation": "C-"+row["id"], "score": scores[i], "bm25": lexical[i], "cosine": semantic.get(i)})
        return {"items": items, "mode": mode, "degraded": degraded, "candidate_count": len(rows)}

    def citation(self, p, citation):
        with self.store.db() as db:
            row = db.execute('SELECT title,document_id,version,section,start,"end",content FROM chunks WHERE id=? AND tenant=? AND (visibility=\'tenant\' OR owner=?)',
                             (citation.removeprefix("C-"), p.tenant, p.user)).fetchone()
            if row is None:
                raise KeyError("Citation not found or document version replaced")
            return dict(row)

    def status(self):
        with self.store.db() as db:
            count = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"embedding": self.embedding.identity if self.embedding else None,
                "backend": "sqlite-exact-cosine+bm25-rrf", "chunks": count}
