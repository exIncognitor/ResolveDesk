import json
import re

from tools.check_release import PATTERNS, local_credentials


def test_release_scan_distinguishes_worker_identity_from_standalone_provider_key():
    key=b'sk-'+b'fixture'*5
    assert any(re.search(pattern,b'"api_key": "'+key+b'"') for pattern in PATTERNS)
    assert not any(re.search(pattern,b'"instance": "desk-'+b'a'*32+b'"') for pattern in PATTERNS)


def test_local_provider_credentials_are_recognized_without_a_known_prefix(tmp_path):
    provider_key = 'fixture_unknown_'+'x'*30
    env_key = 'fixture_env_'+'y'*30
    (tmp_path/'key.txt').write_text(provider_key+'\n', encoding='utf-8')
    (tmp_path/'.env').write_text('OPENCODE_ZEN_API_KEY='+env_key+'\nMODEL_NAME=not-a-secret\n', encoding='utf-8')
    assert local_credentials(tmp_path) == {provider_key.encode(), env_key.encode()}


def test_local_login_passwords_and_encryption_master_key_are_scanned(tmp_path):
    (tmp_path/'data').mkdir()
    runtime = tmp_path/'.codex-runtime'
    runtime.mkdir()
    master_key = b'fixture_master_key_for_release_scanning'
    password = 'fixture_login_password'
    partial_password = 'fixture_partial_setup_password'
    (tmp_path/'data/config-encryption.key').write_bytes(master_key+b'\n')
    (runtime/'login-accounts.json').write_text(json.dumps({'learner':{'password':password}}),encoding='utf-8')
    (runtime/'login-accounts-extra.json').write_text(json.dumps({'support':{'password':partial_password}}),encoding='utf-8')
    assert local_credentials(tmp_path) == {master_key,password.encode(),partial_password.encode()}
