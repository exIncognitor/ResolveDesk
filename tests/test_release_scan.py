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
