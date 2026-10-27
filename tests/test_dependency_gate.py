"""A deployment exclusion must expire and must not hide new audit findings."""
from datetime import date
import json
from pathlib import Path

import pytest

from tools.check_dependencies import ROOT,classify,verify_profile


def policy():return json.loads((ROOT/'deploy/dependency_audit_policy.json').read_text(encoding='utf-8'))


def finding(advisory='GHSA-f4j7-r4q5-qw2c',version='1.5.9'):
    return {'dependencies':[{'name':'chromadb','version':version,'vulns':[{'id':'PYSEC-fixture','aliases':[advisory],'fix_versions':[]}]}]}


def profile_copy(destination,*,public=False,sdk=False):
    files={name:(ROOT/name).read_text(encoding='utf-8') for name in ('compose.services.yaml','compose.yaml','deploy/nginx.conf')}
    for directory in ('resolvedesk','core','agents','tooling','evaluation','memory','monitor','tools'):
        for file in (ROOT/directory).glob('*.py'):files[file.relative_to(ROOT).as_posix()]=file.read_text(encoding='utf-8')
    if public:files['compose.services.yaml']=files['compose.services.yaml'].replace('127.0.0.1:${CHROMA_PORT','0.0.0.0:${CHROMA_PORT')
    if sdk:files['resolvedesk/app.py']+='\nimport tooling.knowledge_base\n'
    for name,text in files.items():
        path=destination/name;path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('x',encoding='utf-8') as output:output.write(text)
    return destination


def test_known_profile_exclusion_is_reported_and_duplicate_aliases_do_not_multiply():
    data=finding();data['dependencies'][0]['vulns']*=2
    result=classify(data,policy(),{'chromadb'},today=date(2026,10,27))
    assert result['status']=='pass_with_profile_exclusions' and not result['active_findings']
    assert len(result['reviewed_profile_exclusions'])==1


@pytest.mark.parametrize('advisory,version',[('GHSA-new-unreviewed-finding','1.5.9'),('GHSA-f4j7-r4q5-qw2c','1.6.0')])
def test_new_advisory_or_version_is_not_silently_excluded(advisory,version):
    result=classify(finding(advisory,version),policy(),{'chromadb'},today=date(2026,10,27))
    assert result['status']=='fail' and result['active_findings']


def test_profile_review_expiration_fails_closed():
    with pytest.raises(ValueError,match='expired'):classify(finding(),policy(),{'chromadb'},today=date(2026,11,27))


def test_public_raw_service_invalidates_profile_exclusion(tmp_path):
    root=profile_copy(tmp_path/'public',public=True)
    with pytest.raises(ValueError,match='loopback'):verify_profile(root)


def test_sdk_indirectly_added_to_main_path_invalidates_profile_exclusion(tmp_path):
    root=profile_copy(tmp_path/'sdk',sdk=True)
    with pytest.raises(ValueError,match='SDK'):verify_profile(root)


def test_unverified_runtime_package_cannot_pass_gate():
    data={'dependencies':[{'name':'chromadb','skip_reason':'audit unavailable'}]}
    result=classify(data,policy(),{'chromadb'},today=date(2026,10,27))
    assert result['status']=='fail' and result['unverified_runtime_packages']


def test_runtime_package_missing_from_audit_cannot_pass_gate():
    result=classify(finding(),policy(),{'chromadb','starlette'},today=date(2026,10,27))
    assert result['status']=='fail'
    assert result['unverified_runtime_packages']==[{'package':'starlette','reason':'Absent from dependency auditor output'}]


@pytest.mark.parametrize('package',['resolvedesk','tooling'])
def test_parent_package_initializer_sdk_invalidates_profile(tmp_path,package):
    root=profile_copy(tmp_path/'initializer')
    path=root/package/'__init__.py'
    path.write_text(path.read_text(encoding='utf-8')+'\nimport chromadb\n',encoding='utf-8')
    with pytest.raises(ValueError,match='SDK'):verify_profile(root)


@pytest.mark.parametrize('call',["__import__('chromadb')","importlib.import_module('chromadb')"])
def test_dynamic_import_requires_fresh_review(tmp_path,call):
    root=profile_copy(tmp_path/'dynamic')
    path=root/'resolvedesk/app.py'
    path.write_text(path.read_text(encoding='utf-8')+'\n'+call+'\n',encoding='utf-8')
    with pytest.raises(ValueError,match='Dynamic'):verify_profile(root)
