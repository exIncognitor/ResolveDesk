"""Explicit service configuration. Secret values never appear in repr/status."""
from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import quote, urlsplit

from dotenv import dotenv_values
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ServiceSettings:
    database_url: str = field(repr=False)
    redis_url: str = field(repr=False)
    chroma_url: str
    namespace: str = "resolvedesk"

    @classmethod
    def load(cls, root=ROOT):
        local = dotenv_values(Path(root)/".codex-runtime"/"services.env")
        def get(name, default=""):
            return os.environ.get(name, local.get(name) or default)
        database = get("RESOLVEDESK_DATABASE_URL")
        if not database:
            password = get("POSTGRES_PASSWORD")
            if not password: raise ValueError("Configure PostgreSQL URL or local services credentials")
            database = URL.create("postgresql+psycopg", username="resolvedesk", password=password,
                host=get("POSTGRES_HOST","127.0.0.1"), port=int(get("POSTGRES_PORT","15432")),database="resolvedesk").render_as_string(hide_password=False)
        redis_url = get("RESOLVEDESK_REDIS_URL")
        if not redis_url:
            password = get("REDIS_PASSWORD")
            if not password: raise ValueError("Configure Redis URL or local services credentials")
            redis_url = "redis://:"+quote(password,safe="")+"@"+get("REDIS_HOST","127.0.0.1")+":"+get("REDIS_PORT","16379")+"/0"
        if not database.startswith(("postgresql://","postgresql+psycopg://")):
            raise ValueError("Service mode requires PostgreSQL; SQLite is an explicit offline fixture only")
        if urlsplit(redis_url).scheme not in {"redis","rediss"}: raise ValueError("Invalid Redis URL")
        chroma = get("RESOLVEDESK_CHROMA_URL", "http://127.0.0.1:"+get("CHROMA_PORT","18001"))
        parsed = urlsplit(chroma)
        if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Invalid Chroma URL")
        return cls(database, redis_url, chroma)
