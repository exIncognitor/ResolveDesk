"""Local deployed login/ACL checks. No model requests or business mutations.

Creates and logs out temporary sessions for the existing local accounts. Database
verification is read-only; credentials are consumed locally and never reported.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

from cryptography.fernet import Fernet
import httpx
import psycopg
from sqlalchemy.engine import make_url

from resolvedesk.settings import ServiceSettings
from tools.check_release import local_credentials

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:18080'


def main():
    accounts = json.loads((ROOT/'.codex-runtime/login-accounts.json').read_text(encoding='utf-8'))
    private = local_credentials(ROOT)
    checks = {}
    with httpx.Client(base_url=BASE, timeout=20, trust_env=False) as http:
        sessions = {}
        try:
            checks['ready'] = http.get('/ready').json().get('ready') is True
            checks['unauthenticated_rejected'] = http.get('/admin/models').status_code == 401
            for user in ('learner', 'support'):
                account = accounts[user]
                response = http.post('/auth/login', json={k:account[k] for k in ('tenant','username','password')})
                response.raise_for_status()
                session = response.json()
                sessions[user] = {'Authorization':'Bearer '+session['token']}
                private.add(session['token'].encode())
                checks[user+'_password_login'] = session['principal']['role'] == account['role']
                checks[user+'_no_store'] = 'no-store' in response.headers.get('cache-control','').lower().split(', ')
            def request(method, path, body=None, user='support'):
                return http.request(method, path, json=body, headers=sessions[user])
            for path in ('/admin/models','/admin/model-usage','/admin/accounts','/admin/audit',
                         '/system','/knowledge/publications','/eval/datasets'):
                checks['employee_denied:'+path] = request('GET',path,user='learner').status_code == 403
            # All payloads are valid; denial must come from authorization, not input validation.
            denied_operations = [
                ('POST','/admin/models',{'name':'ACL rejection probe','protocol':'openai','model':'fixture',
                    'base_url':'https://api.deepseek.com/v1','api_key':'fixture-only-key','currency':'CNY','prices':{}}),
                ('POST','/admin/models/environment/activate',{'revision':0}),
                ('PATCH','/admin/accounts/support',{'enabled':False}),
                ('POST','/admin/accounts/support/revoke-sessions',None),
                ('POST','/admin/accounts',{'username':'acl-denial-probe','password':'fixture-only-password','role':'support'})]
            for method,path,body in denied_operations:
                checks['employee_denied:'+method+' '+path] = request(method,path,body,'learner').status_code == 403
            configurations = request('GET','/admin/models')
            configurations.raise_for_status()
            config = configurations.json()
            checks['support_models_visible'] = bool(config['profiles'])
            checks['active_flash_profile'] = config['active']['model']=='deepseek-flash' and config['active']['configured']
            public_fields = {'id','name','protocol','model','base_url','currency','prices','revision','configured'}
            checks['model_response_has_only_public_fields'] = all(set(p)<=public_fields for p in config['profiles']+[config['active']])
            users = request('GET','/admin/accounts')
            users.raise_for_status()
            checks['support_accounts_visible'] = {row['user_id'] for row in users.json()} >= {'learner','support'}
            checks['password_hash_not_returned'] = all('password_hash' not in row for row in users.json())
            audit = request('GET','/admin/audit')
            audit.raise_for_status()
            checks['model_audit_persisted'] = {'model.create','model.activate'} <= {row['action'] for row in audit.json()}
            usage = request('GET','/admin/model-usage')
            usage.raise_for_status()
            checks['no_live_model_calls_added'] = usage.json()['calls'] == 0
            checks['no_secret_in_management_responses'] = all(not any(value in r.content for value in private)
                for r in (configurations,users,audit,usage))
            html = http.get('/').text
            scripts = re.findall(r'src="(/assets/[^\"]+\.js)"',html)
            container_html = subprocess.check_output(['docker','compose','--env-file','.codex-runtime/services.env',
                'exec','-T','app','cat','/app/frontend/dist/index.html'],cwd=ROOT,text=True,encoding='utf-8')
            checks['current_frontend_served'] = len(scripts)==1 and scripts[0] in container_html and http.get(scripts[0]).status_code==200
            mcp_body = {'jsonrpc':'2.0','id':1,'method':'initialize',
                'params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'local-acceptance','version':'1'}}}
            mcp_headers = dict(sessions['learner'],Accept='application/json, text/event-stream')
            checks['knowledge_mcp_accepts_session'] = http.post('/mcp/knowledge/',json=mcp_body,headers=mcp_headers).status_code==200
            checks['logout_succeeded'] = request('POST','/auth/logout',user='learner').status_code==200
            checks['http_rejects_logged_out_session'] = request('GET','/me',user='learner').status_code==401
            checks['mcp_rejects_logged_out_session'] = http.post('/mcp/knowledge/',json=mcp_body,headers=mcp_headers).status_code==401
        finally:
            for headers in sessions.values():
                http.post('/auth/logout',headers=headers)
    url = make_url(ServiceSettings.load().database_url)
    with psycopg.connect(host=url.host,port=url.port,user=url.username,password=url.password,dbname=url.database,
                         connect_timeout=5,options='-c default_transaction_read_only=on') as db:
        with db.cursor() as cur:
            cur.execute('SELECT status,COUNT(*) FROM resolvedesk_finance.paid_calls GROUP BY status ORDER BY status')
            finance = dict(cur.fetchall())
            cur.execute("SELECT SUM(CASE WHEN status='settled' THEN charged ELSE reserved END) FROM resolvedesk_finance.paid_calls")
            commitment = int(cur.fetchone()[0])
            checks['historical_finance_preserved'] = finance=={'settled':2315} and commitment==10775836
            cur.execute('SELECT key_cipher FROM model_profiles WHERE id=%s AND tenant=%s',(config['active']['id'],'demo'))
            cipher = cur.fetchone()[0]
            plaintext = Fernet((ROOT/'data/config-encryption.key').read_bytes().strip()).decrypt(cipher.encode())
            checks['model_key_encrypted_at_rest'] = bool(plaintext) and plaintext.decode()!=cipher
            checks['plaintext_key_not_returned'] = plaintext not in configurations.content
    revision = subprocess.check_output(['docker','image','inspect','resolvedesk-app:local','--format',
        '{{index .Config.Labels "org.opencontainers.image.revision"}}'],cwd=ROOT,text=True).strip()
    report = {'tested_at':datetime.now(timezone.utc).isoformat(),'base_url':BASE,'runtime_revision':revision,
              'checks':checks,'passed':sum(checks.values()),'total':len(checks),'model_calls_submitted':0,
              'business_mutations_submitted':0,'historical_finance':finance,'historical_commitment_micro_cny':commitment}
    path = ROOT/'reports'/('enterprise-login-deployment-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    with path.open('x',encoding='utf-8') as out: json.dump(report,out,ensure_ascii=False,indent=2)
    print(json.dumps({'passed':report['passed'],'total':report['total'],'failed':[k for k,v in checks.items() if not v],
                      'evidence':str(path.relative_to(ROOT))},ensure_ascii=False))
    if not all(checks.values()): raise SystemExit(1)


if __name__=='__main__': main()
