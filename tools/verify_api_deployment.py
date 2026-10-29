"""Free G11 contract/upload/CLI acceptance against an already deployed server."""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx

ROOT=Path(__file__).resolve().parents[1]

# Fixed 17 original decorator endpoints; changed contracts are documented in
# docs/API_CLI_MIGRATION_2026-10-27.md rather than silently emulated.
MAPPING={
    'GET /health':'GET /health','GET /skills':'GET /skills','POST /skills/reload':'POST /skills/reload',
    'POST /chat':'POST /runs','GET /monitor':'GET /monitor','GET /trace/tool/{request_id}':'GET /trace/tool/{run_id}',
    'GET /trace/tools':'GET /trace/tools','GET /metrics':'GET /metrics','POST /search':'POST /knowledge/search',
    'POST /knowledge/add':'POST /knowledge/add','POST /knowledge/upload':'POST /knowledge/upload',
    'GET /knowledge/stats':'GET /knowledge/stats','POST /eval/run':'POST /eval/run','GET /eval/jobs/{job_id}':'GET /eval/jobs/{job_id}',
    'GET /integrations/embedding':'GET /integrations/embedding','GET /integrations/mcp':'GET /integrations/mcp',
    'POST /integrations/mcp/call':'POST /integrations/mcp/call'}


def main():
    base=sys.argv[1] if len(sys.argv)>1 else 'http://127.0.0.1:18080'
    if base not in {'http://127.0.0.1:8080','http://127.0.0.1:18080'}:raise ValueError('Local acceptance only')
    access=json.loads((ROOT/'.codex-runtime/access.json').read_text(encoding='utf-8'))
    checks={}
    with httpx.Client(base_url=base,timeout=30,trust_env=False) as http:
        def request(method,path,body=None,actor='support'):
            result=http.request(method,path,json=body,headers={'Authorization':'Bearer '+access[actor]['token']})
            result.raise_for_status();return result.json()
        before=request('GET','/system')['model']
        schema=http.get('/openapi.json').json()
        checks['all_seventeen_have_current_replacement']=len(MAPPING)==17 and all(method.lower() in schema['paths'].get(path,{}) for method,path in (entry.split(' ',1) for entry in MAPPING.values()))
        checks['server_multipart_schema']=schema['paths']['/knowledge/upload']['post']['requestBody']['content']['multipart/form-data']['schema']['properties']['file']['format']=='binary'
        checks['intent_alias_is_current']=request('GET','/integrations/embedding')==request('GET','/integrations/intent-embedding')
        recent=request('GET','/trace/tools')
        checks['recent_owner_scope']=recent['scope']=='own_turns' and isinstance(recent['items'],list)
        denied=http.post('/knowledge/add',json={'documents':[]},headers={'Authorization':'Bearer '+access['learner']['token']})
        checks['batch_requires_support']=denied.status_code==403
        invalid=http.post('/knowledge/add',json={'documents':[]},headers={'Authorization':'Bearer '+access['support']['token']})
        checks['batch_validation']=invalid.status_code==422
        # Both tests reject before importing, and create no knowledge/business data.
        headers={'Authorization':'Bearer '+access['support']['token']}
        unsupported=http.post('/knowledge/upload',files={'file':('unsupported.exe',b'x'*(1100*1024))},headers=headers)
        checks['gateway_allows_more_than_old_one_mib']=unsupported.status_code==415
        oversized=http.post('/knowledge/upload',files={'file':('oversized.md',b'x'*(10*1024*1024+1))},headers=headers)
        checks['server_ten_mib_boundary']=oversized.status_code==413 and 'application/json' in oversized.headers.get('content-type','')
        env=os.environ.copy();env['RESOLVEDESK_API_TOKEN']=access['learner']['token'];env['PYTHONUTF8']='1'
        cli=subprocess.run([sys.executable,'-X','utf8','-B','-m','resolvedesk.cli','--base-url',base],cwd=ROOT,env=env,
            input=':integrations\n:search VPN\n:recent\n:quit\n',capture_output=True,text=True,encoding='utf-8',timeout=30)
        checks['real_interactive_cli_reads_current_server']=cli.returncode==0 and '"mcp"' in cli.stdout and '"document_id"' in cli.stdout and '网络或等待中断' not in cli.stdout and '接口返回' not in cli.stdout
        checks['cli_does_not_print_api_token']=all(actor['token'] not in cli.stdout+cli.stderr for actor in access.values())
        after=request('GET','/system')['model'];checks['model_configuration_unchanged']=before==after
    report={'base':base,'mapping':MAPPING,'checks':checks,'passed':sum(checks.values()),'total':len(checks),'model_configuration_changed':False,'model_configuration':after,'cli_exit_code':cli.returncode}
    path=ROOT/'reports'/('api-cli-deployment-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    with path.open('x',encoding='utf-8') as dest:json.dump(report,dest,ensure_ascii=False,indent=2)
    print(json.dumps({'passed':report['passed'],'total':report['total'],'evidence':str(path.relative_to(ROOT))}))
    if not all(checks.values()):raise SystemExit(1)


if __name__=='__main__':main()
