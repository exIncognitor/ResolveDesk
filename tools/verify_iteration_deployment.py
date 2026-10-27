"""Read-only normal deployment checks; no model calls or parser attack inputs."""
from datetime import datetime,timezone
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import subprocess
import sys

import httpx

ROOT=Path(__file__).resolve().parents[1]


def main():
    access=json.loads((ROOT/'.codex-runtime/access.json').read_text(encoding='utf-8'))
    paths=[]
    for folder in ('resolvedesk','agents','core','tooling','knowledge','skills'):
        paths.extend(path for path in sorted((ROOT/folder).rglob('*')) if path.is_file() and not {'history','__pycache__'}&set(path.relative_to(ROOT).parts))
    paths.extend(ROOT/name for name in ('requirements.txt','Dockerfile','deploy/mcp_servers.json'))
    names=[path.relative_to(ROOT).as_posix() for path in paths]
    expected={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    code="import hashlib,json,sys;from pathlib import Path;print(json.dumps({n:hashlib.sha256(Path('/app',n).read_bytes()).hexdigest() for n in json.loads(sys.argv[1])}))"
    image=json.loads(subprocess.check_output(['docker','exec','resolvedesk-services-app-1','python','-c',code,json.dumps(names)],text=True,encoding='utf-8',timeout=30))
    checks={'all_runtime_image_source_hashes_match':image==expected}
    deps=('fastapi','starlette','python-multipart','mcp','anthropic','numpy','onnxruntime','tokenizers')
    code="import json,sys,importlib.metadata as m;print(json.dumps({n:m.version(n) for n in json.loads(sys.argv[1])}))"
    installed=json.loads(subprocess.check_output(['docker','exec','resolvedesk-services-app-1','python','-c',code,json.dumps(deps)],text=True,encoding='utf-8',timeout=30))
    local={name:metadata.version(name) for name in deps}
    checks['image_and_host_key_dependency_versions_match']=installed==local
    pip=subprocess.run(['docker','exec','resolvedesk-services-app-1','python','-m','pip','check'],capture_output=True,text=True,timeout=30)
    checks['image_pip_check']=pip.returncode==0
    report={'checks':checks,'source_sha256':expected,'image_dependencies':installed,'host_dependencies':local,'entrances':[],
        'image_revision':subprocess.check_output(['docker','inspect','--format','{{ index .Config.Labels "org.opencontainers.image.revision" }}','resolvedesk-services-app-1'],text=True,encoding='utf-8').strip(),
        'note':'Normal authenticated read/CLI requests only. Upload fault/security reproductions were run in isolated copies. No external notification was sent.'}
    index=(ROOT/'frontend/dist/index.html').read_bytes()
    assets=re.findall(r'(?:src|href)="(/assets/[^\"]+)"',index.decode())
    for port in (8080,18080):
        base='http://127.0.0.1:'+str(port);prefix=str(port)+':'
        with httpx.Client(base_url=base,headers={'Authorization':'Bearer '+access['support']['token']},trust_env=False,timeout=30) as http:
            def get(path):
                response=http.get(path);response.raise_for_status();return response.json()
            before=get('/system')['budget'];schema=get('/openapi.json');catalog=get('/service-catalog')
            checks[prefix+'ready']=get('/ready')['ready']
            actions=schema['components']['schemas']['ServiceAction']['properties']['action']['enum']
            checks[prefix+'license_action_contract']={'reserve','allocate','revoke'}<=set(actions)
            seat=next(item for item in catalog['licenses'] if item['product']=='IntelliJ IDEA Ultimate')
            checks[prefix+'persistent_inventory_contract']=seat['total_seats']==seat['available_seats']+seat['reserved_seats']+seat['allocated_seats'] and seat['reservation_seconds']==900 and seat['simulation'] is True
            specialists=get('/system')['specialists']
            checks[prefix+'model_has_no_license_write_tools']=all(not {'approve','reserve','allocate','revoke'}&set(role['tools']) for role in specialists.values())
            page=http.get('/');page.raise_for_status();checks[prefix+'current_index']=page.content==index
            checks[prefix+'current_js_css']=bool(assets) and all(http.get(name).content==(ROOT/'frontend/dist'/name.lstrip('/')).read_bytes() for name in assets)
            cli=subprocess.run([sys.executable,'-X','utf8','-B','-m','resolvedesk.cli','--base-url',base,'--actor','learner'],input=':help\n:search VPN\n:quit\n',capture_output=True,text=True,encoding='utf-8',cwd=ROOT,timeout=45)
            checks[prefix+'real_cli_read']=cli.returncode==0 and 'ResolveDesk CLI' in cli.stdout and 'C-' in cli.stdout
            after=get('/system')['budget'];checks[prefix+'free_checks_do_not_spend']=before==after
            report['entrances'].append({'base':base,'budget':after,'inventory':seat})
    prometheus=httpx.get('http://127.0.0.1:19090/api/v1/query',params={'query':'up{job="resolvedesk"}'},trust_env=False,timeout=15).json()
    checks['prometheus_up']=prometheus['status']=='success' and bool(prometheus['data']['result']) and all(item['value'][1]=='1' for item in prometheus['data']['result'])
    report.update(passed=sum(checks.values()),total=len(checks))
    path=ROOT/'reports'/('iteration2-deployment-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    with path.open('x',encoding='utf-8') as out:json.dump(report,out,ensure_ascii=False,indent=2)
    print(json.dumps({'passed':report['passed'],'total':report['total'],'failed':[name for name,value in checks.items() if not value],'evidence':str(path.relative_to(ROOT))},ensure_ascii=False))
    if report['passed']!=report['total']:raise SystemExit(1)


if __name__=='__main__':main()
