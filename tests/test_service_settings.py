from urllib.parse import urlsplit

from resolvedesk.settings import ServiceSettings


def test_legacy_env_does_not_redirect_new_dependency_ports(tmp_path,monkeypatch):
    runtime=tmp_path/".codex-runtime";runtime.mkdir()
    (runtime/"services.env").write_text("POSTGRES_PASSWORD=test\nREDIS_PASSWORD=test\nCHROMA_PORT=18001\n",encoding="utf-8")
    for name in ("RESOLVEDESK_DATABASE_URL","RESOLVEDESK_REDIS_URL","RESOLVEDESK_CHROMA_URL"): monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv("CHROMA_PORT","8000")
    settings=ServiceSettings.load(tmp_path)
    assert settings.chroma_url=="http://127.0.0.1:18001"
    monkeypatch.setenv("RESOLVEDESK_CHROMA_URL","http://chroma:8000")
    assert ServiceSettings.load(tmp_path).chroma_url=="http://chroma:8000"
