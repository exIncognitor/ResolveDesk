"""Audit installed dependencies without hiding reviewed deployment exclusions."""
from __future__ import annotations

import ast
from datetime import date
import importlib.metadata as metadata
import json
from pathlib import Path
import subprocess
import sys

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import yaml

ROOT=Path(__file__).resolve().parents[1]


def runtime_requirements(root=ROOT):
    required={};pending=[]
    for line in (root/'requirements.txt').read_text(encoding='utf-8').splitlines():
        line=line.split('#',1)[0].strip()
        if not line:continue
        requirement=Requirement(line)
        if len(requirement.specifier)!=1 or not str(requirement.specifier).startswith('==') or '*'in str(requirement.specifier):
            raise ValueError('Runtime dependencies must use exact tested versions')
        if requirement.marker and not requirement.marker.evaluate():continue
        if not requirement.specifier.contains(metadata.version(requirement.name)):raise ValueError('Installed direct dependency does not match manifest: '+requirement.name)
        pending.append(requirement)
    while pending:
        requirement=pending.pop();name=canonicalize_name(requirement.name)
        extras=set(requirement.extras)
        if name in required and extras<=required[name]:continue
        required.setdefault(name,set()).update(extras)
        for item in metadata.requires(name) or []:
            dependency=Requirement(item)
            if dependency.marker is None or any(dependency.marker.evaluate({'extra':extra}) for extra in {''}|required[name]):pending.append(dependency)
    return set(required)


def current_main_graph(root):
    def local_files(module):
        parts=module.split('.')
        for index in range(1,len(parts)+1):
            initializer=root.joinpath(*parts[:index])/'__init__.py'
            if initializer.is_file():yield initializer
        path=root.joinpath(*parts).with_suffix('.py')
        if path.is_file():yield path
    pending=list(local_files('resolvedesk.app'));seen=set()
    while pending:
        file=pending.pop()
        if file in seen:continue
        seen.add(file);tree=ast.parse(file.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            modules=[]
            if isinstance(node,ast.Import):modules=[alias.name for alias in node.names]
            elif isinstance(node,ast.ImportFrom):
                module=node.module or ''
                if node.level:
                    prefix=list(file.relative_to(root).parent.parts)[:len(file.relative_to(root).parent.parts)-node.level+1]
                    module='.'.join(prefix+module.split('.'))
                modules=[module]+[module+'.'+alias.name for alias in node.names]
            elif isinstance(node,ast.Call) and (
                isinstance(node.func,ast.Name) and node.func.id in {'__import__','import_module'} or
                isinstance(node.func,ast.Attribute) and node.func.attr=='import_module'
            ):
                raise ValueError('Dynamic imports require fresh deployment profile review')
            for module in modules:
                if module=='chromadb' or module.startswith('chromadb.'):raise ValueError('Chroma SDK is reachable from the main entry')
                pending.extend(local_files(module))
    return sorted(str(p.relative_to(root)) for p in seen)


def verify_profile(root=ROOT):
    services=yaml.safe_load((root/'compose.services.yaml').read_text(encoding='utf-8'))['services']
    chroma=services['chroma'];overlay=yaml.safe_load((root/'compose.yaml').read_text(encoding='utf-8'))['services']
    if chroma['image']!='chromadb/chroma:1.5.9' or any(key in chroma for key in ('command','entrypoint')) or overlay.get('chroma'):
        raise ValueError('Reviewed stock Rust server configuration changed')
    if not chroma.get('ports') or any(not isinstance(p,str) or not p.startswith('127.0.0.1:') for p in chroma['ports']):raise ValueError('Raw Chroma must remain loopback-only')
    if set(chroma.get('environment',{}))-{'ANONYMIZED_TELEMETRY'}:raise ValueError('Chroma backend/auth configuration needs fresh review')
    if 'chroma' in (root/'deploy/nginx.conf').read_text(encoding='utf-8').lower():raise ValueError('Raw Chroma proxy requires fresh review')
    return {'profile':'local-compose-rust-explicit-vectors','main_module_graph':current_main_graph(root),'raw_chroma_loopback_only':True}


def classify(audit,policy,runtime_names,*,root=ROOT,today=None):
    today=today or date.today()
    if date.fromisoformat(policy['expires_on'])<today:raise ValueError('Dependency profile review expired')
    exclusions=policy['exclusions'];profile=verify_profile(root)
    active=[];reviewed=[];outside=[];skipped=[];seen=set();covered=set()
    for dependency in audit['dependencies']:
        name=canonicalize_name(dependency['name'])
        covered.add(name)
        if dependency.get('skip_reason'):
            if name in runtime_names:skipped.append({'package':name,'reason':dependency['skip_reason']})
            continue
        for vuln in dependency.get('vulns',[]):
            identities={vuln['id'],*vuln.get('aliases',[])}
            canonical=next((v for v in sorted(identities) if v.startswith('GHSA-')),vuln['id'])
            key=(name,dependency['version'],canonical)
            if key in seen:continue
            seen.add(key)
            item={'package':name,'version':dependency['version'],'advisory':canonical,'fix_versions':vuln.get('fix_versions',[])}
            if name not in runtime_names and name not in {'pip','pip-audit'}:outside.append(item);continue
            match=next((entry for entry in exclusions if entry['package']==name and entry['version']==dependency['version'] and entry['advisory'] in identities),None)
            if match:reviewed.append({**item,'classification':match['classification'],'reason':match['reason'],'expires_on':policy['expires_on']})
            else:active.append(item)
    skipped.extend({'package':name,'reason':'Absent from dependency auditor output'} for name in sorted(runtime_names-covered))
    return {'status':'fail' if active or skipped else 'pass_with_profile_exclusions' if reviewed else 'pass',
        'active_findings':active,'reviewed_profile_exclusions':reviewed,'outside_runtime_findings':outside,
        'unverified_runtime_packages':skipped,'runtime_dependency_count':len(runtime_names),'deployment_contract':profile,
        'note':'Profile exclusions are not upstream fixes; raw audit findings remain visible.'}


def main():
    policy=json.loads((ROOT/'deploy/dependency_audit_policy.json').read_text(encoding='utf-8'))
    required=runtime_requirements()
    result=subprocess.run([sys.executable,'-X','utf8','-B','-m','pip_audit','--format','json','--progress-spinner','off','--strict','--aliases','on','--desc','off','--timeout','20'],capture_output=True,text=True)
    if result.returncode not in {0,1}:raise RuntimeError('Dependency auditor failed; no clean result is claimed')
    try:audit=json.loads(result.stdout)
    except ValueError:raise RuntimeError('Dependency audit did not return valid evidence') from None
    report=classify(audit,policy,required)
    print(json.dumps(report,ensure_ascii=False))
    if report['status']=='fail':raise SystemExit(1)


if __name__=='__main__':main()
