import re

from tools.check_release import PATTERNS


def test_release_scan_distinguishes_worker_identity_from_standalone_provider_key():
    key=b'sk-'+b'fixture'*5
    assert any(re.search(pattern,b'"api_key": "'+key+b'"') for pattern in PATTERNS)
    assert not any(re.search(pattern,b'"instance": "desk-'+b'a'*32+b'"') for pattern in PATTERNS)
