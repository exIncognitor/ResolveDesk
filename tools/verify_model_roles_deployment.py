"""Read-only localhost acceptance; never submits a model request or business mutation."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

import httpx
import psycopg
from sqlalchemy.engine import make_url

from resolvedesk.settings import ServiceSettings
from tools.check_release import local_credentials

ROOT = Path(__file__).resolve().parents[1]


def main():
    access = json.loads((ROOT/'.codex-runtime/access.json').read_text(encoding='utf-8'))
    secrets = local_credentials(ROOT)
    results = []
    for base in ('http://127.0.0.1:8080', 'http://127.0.0.1:18080'):
        checks = {}
        with httpx.Client(base_url=base, timeout=20, trust_env=False) as http:
            def get(path, actor='support'):
                return http.get(path, headers={'Authorization':'Bearer '+access[actor]['token']})
            ready = http.get('/ready')
            checks['ready'] = ready.status_code == 200 and ready.json()['ready'] is True
            config = get('/system')
            config.raise_for_status()
            data = config.json()
            checks['configured_model'] = data['model']['configured'] and data['model']['model'] == 'deepseek-flash'
            checks['runtime_has_no_budget_gate'] = 'budget' not in data and 'usage' in data
            checks['no_key_in_configuration'] = not any(secret in config.content for secret in secrets)
            checks['employee_identity'] = get('/me','learner').json()['role'] == 'user'
            checks['operator_identity'] = get('/me').json()['role'] == 'support'
            for path in ('/system','/mcp','/integrations/mcp','/integrations/embedding','/metrics',
                         '/knowledge/publications','/evaluation/candidates','/eval/datasets','/eval/retention'):
                checks['employee_denied:'+path] = get(path,'learner').status_code == 403
            checks['support_connections_available'] = get('/mcp').status_code == 200
            checks['employee_catalog_is_simulation'] = get('/service-catalog','learner').json()['simulation'] is True
            checks['employee_observations_are_owner_scoped'] = get('/monitor','learner').json()['scope'] == 'own_observed_executions'
            html = http.get('/').text
            scripts = re.findall(r'src="(/assets/[^\"]+\.js)"',html)
            local = (ROOT/'frontend/dist/index.html').read_text(encoding='utf-8')
            checks['current_frontend_bundle'] = len(scripts) == 1 and scripts[0] in local
            checks['current_frontend_served'] = bool(scripts) and http.get(scripts[0]).status_code == 200
        results.append({'base':base,'model':data['model'],'checks':checks})
    url = make_url(ServiceSettings.load().database_url)
    with psycopg.connect(host=url.host,port=url.port,user=url.username,password=url.password,dbname=url.database,
                         connect_timeout=5,options='-c default_transaction_read_only=on') as db:
        with db.cursor() as cur:
            cur.execute('SELECT status,COUNT(*) FROM resolvedesk_finance.paid_calls GROUP BY status ORDER BY status')
            historical_finance = dict(cur.fetchall())
            cur.execute('SELECT SUM(CASE WHEN status=\'settled\' THEN charged ELSE reserved END) FROM resolvedesk_finance.paid_calls')
            historical_commitment = int(cur.fetchone()[0])
    image_revision = subprocess.check_output(['docker','image','inspect','resolvedesk-app:local',
        '--format','{{index .Config.Labels "org.opencontainers.image.revision"}}'],cwd=ROOT,text=True).strip()
    report = {'model_calls_submitted':0,'business_mutations_submitted':0,'results':results,
              'historical_finance':historical_finance,'historical_commitment_micro_cny':historical_commitment,
              'image_revision':image_revision}
    path = ROOT/'reports'/('model-role-deployment-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    with path.open('x',encoding='utf-8') as dest:
        json.dump(report,dest,ensure_ascii=False,indent=2)
    passed = sum(sum(result['checks'].values()) for result in results)
    total = sum(len(result['checks']) for result in results)
    print(json.dumps({'passed':passed,'total':total,'evidence':str(path.relative_to(ROOT))}))
    if passed != total:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
