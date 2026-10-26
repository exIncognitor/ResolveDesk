"""Check tracked/staged content without printing any credential values."""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'history', '.venv', 'venv', 'node_modules', 'data', '.codex-runtime', '.codex-tmp',
            '__pycache__', '.pytest_cache', 'logs', '.idea'}
PATTERNS = [rb'sk-[A-Za-z0-9_-]{20,}', rb'gh[pousr]_[A-Za-z0-9]{20,}',
            rb'github_pat_[A-Za-z0-9_]{20,}', rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--staged', action='store_true')
    args = parser.parse_args()
    cmd = ['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z'] if args.staged else ['git', 'ls-files', '-z']
    paths = [p for p in subprocess.check_output(cmd, cwd=ROOT).decode('utf-8').split('\0') if p]
    local_secrets = []
    for name in ('.env', 'key.txt'):
        p = ROOT / name
        if p.is_file():
            local_secrets += re.findall(rb'sk-[A-Za-z0-9_-]{16,}', p.read_bytes())
    findings = []
    for name in paths:
        p = PurePosixPath(name)
        if (EXCLUDED.intersection(p.parts) or p.name in {'.env', 'key.txt'} or p.suffix in {'.key', '.pem'}
                or (p.name.startswith('.env.') and p.name != '.env.example')
                or p.parts[:2] == ('frontend', 'dist')):
            findings.append({'path': name, 'reason': 'excluded local data or artifact'})
            continue
        raw = subprocess.check_output(['git', 'show', ':'+name], cwd=ROOT) if args.staged else (ROOT/name).read_bytes()
        if len(raw) > 50_000_000:
            findings.append({'path': name, 'reason': 'unexpected large file'})
        if any(re.search(pattern, raw) for pattern in PATTERNS) or any(secret in raw for secret in local_secrets):
            findings.append({'path': name, 'reason': 'credential-like content'})
    print(json.dumps({'checked_files': len(paths), 'findings': findings}, ensure_ascii=False))
    if findings:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
