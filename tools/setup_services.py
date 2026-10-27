"""Create new ignored development credentials only; never overwrite secrets."""
from pathlib import Path
import json
import secrets

ROOT=Path(__file__).resolve().parents[1]


def main():
    directory=ROOT/".codex-runtime"
    for part in (directory,*directory.parents):
        if part.is_symlink() or part.is_junction(): raise ValueError("Credential directory must not follow links")
    directory.mkdir(exist_ok=True)
    path=directory/"services.env"
    if not path.exists():
        with path.open("x",encoding="utf-8") as dest:
            dest.write("POSTGRES_PASSWORD="+secrets.token_urlsafe(36)+"\nREDIS_PASSWORD="+secrets.token_urlsafe(36)+"\nPOSTGRES_PORT=15432\nREDIS_PORT=16379\nCHROMA_PORT=18001\n")
    access=directory/"access.json"
    if access.exists():
        credentials=json.loads(access.read_text(encoding="utf-8"))
        token=credentials["support"]["token"]
        target=directory/"prometheus"
        if target.is_symlink() or target.is_junction(): raise ValueError("Credential directory must not follow links")
        target.mkdir(exist_ok=True)
        target=target/"support.token"
        if target.is_symlink() or target.is_junction(): raise ValueError("Credential file must not follow links")
        if not target.exists():
            with target.open("x",encoding="utf-8") as dest: dest.write(token)
        elif target.read_text(encoding="utf-8")!=token:
            raise RuntimeError("Existing Prometheus credential differs; refusing overwrite")
    print("Development service credentials ready. Values are never printed.")


if __name__=="__main__": main()
