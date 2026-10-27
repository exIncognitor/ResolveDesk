"""Initial local accounts, pinned embedding download and synthetic IT policies."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-model", action="store_true")
    parser.add_argument("--offline-fixture",action="store_true",help="Explicit SQLite test fixture; not the live service")
    args = parser.parse_args()
    from resolvedesk.retrieval import HybridKnowledge, LocalEmbedding, download_embedding
    from resolvedesk.store import Principal, Store
    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    auth_path = data / "auth.json"
    access_path = ROOT / ".codex-runtime" / "access.json"
    if not auth_path.exists():
        if access_path.exists():
            raise RuntimeError("Credential files are inconsistent; refusing to overwrite")
        access_path.parent.mkdir(exist_ok=True)
        accounts, access = {}, {}
        for user, role in (("learner", "user"), ("support", "support")):
            token = secrets.token_urlsafe(36)
            accounts[hashlib.sha256(token.encode()).hexdigest()] = {"tenant": "demo", "user": user, "role": role}
            access[user] = {"token": token, "tenant": "demo", "role": role}
        with auth_path.open("x", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2)
        with access_path.open("x", encoding="utf-8") as f:
            json.dump(access, f, indent=2)
        print("Created local credentials:", access_path)
        # Tokens are never printed to logs or placed in project source.
    else:
        print("Existing local accounts retained.")
    model_dir = data / "models" / "multilingual-e5-small"
    if args.download_model:
        download_embedding(model_dir)
    try:
        embedding = LocalEmbedding(model_dir)
    except Exception as exc:
        if args.download_model or not args.offline_fixture:
            raise
        embedding = None
        print("Semantic embedding unavailable:", type(exc).__name__, "(BM25 only)")
    vectors=None
    if args.offline_fixture:
        store=Store(data/"offline-fixture.sqlite3")
    else:
        from resolvedesk.settings import ServiceSettings
        from resolvedesk.vectors import ChromaVectors
        settings=ServiceSettings.load()
        store=Store(settings.database_url)
        vectors=ChromaVectors(settings.chroma_url,embedding.identity)
    from resolvedesk.business import BusinessService
    BusinessService(store).seed_demo()
    kb = HybridKnowledge(store,embedding,vectors)
    principal = Principal("demo", "support", "support")
    for path in sorted((ROOT / "knowledge").glob("*.md")):
        # Unchanged documents with the same embedding are idempotent, so this is safe to rerun.
        content = path.read_text(encoding="utf-8")
        title=content.splitlines()[0].lstrip("# ")
        from resolvedesk.store import encode
        version=hashlib.sha256(encode([title,content]).encode()).hexdigest()[:16]
        legacy_version=hashlib.sha256(content.encode()).hexdigest()[:16]
        with store.db() as db:
            row = db.execute("SELECT version,embedding,title FROM chunks WHERE tenant=? AND document_id=? LIMIT 1", ("demo", path.stem)).fetchone()
        identity = embedding.identity if embedding else None
        if row and row["version"] in {version,legacy_version} and row["embedding"]==identity and row["title"]==title:
            continue
        print(kb.add(principal,path.stem,title,content))
    print("Local setup ready:", kb.status())
    if vectors: vectors.close()
    store.close()


if __name__ == "__main__":
    main()
