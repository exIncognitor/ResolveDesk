"""Read-only real dependency checks; never print connection credentials."""
from pathlib import Path
import json
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    import chromadb
    import redis
    from sqlalchemy import create_engine, text
    from urllib.parse import urlsplit
    from resolvedesk.settings import ServiceSettings
    settings = ServiceSettings.load()
    cache = redis.Redis.from_url(settings.redis_url,socket_connect_timeout=5,socket_timeout=5)
    engine = create_engine(settings.database_url,connect_args={"connect_timeout":5})
    parsed = urlsplit(settings.chroma_url)
    vectors = chromadb.HttpClient(host=parsed.hostname,port=parsed.port or (443 if parsed.scheme=="https" else 80),ssl=parsed.scheme=="https",settings=chromadb.Settings(anonymized_telemetry=False))
    try:
        with engine.connect() as db:
            postgres = db.execute(text("SELECT current_setting('server_version')")).scalar_one()
        report = {"redis_ping":cache.ping(),"redis_version":cache.info("server")["redis_version"],
                  "postgres_version":postgres,"chroma_heartbeat":vectors.heartbeat(),"chroma_version":vectors.get_version()}
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:
        cache.close();engine.dispose()


if __name__=="__main__":main()
