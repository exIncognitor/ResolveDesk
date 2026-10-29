"""Synthetic authorization matrix for employee, operator, owner and tenant boundaries."""
import hashlib

import pytest
from fastapi.testclient import TestClient

from knowledge_fixtures import publish_fixture
from resolvedesk.agent import DeskAgent
from resolvedesk.app import create_app
from resolvedesk.business import BusinessService
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.store import Conflict, Principal, Store
from test_it_workflows import REPAIR, USER, SUPPORT, submit, action
from test_resolvedesk import client_with


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path/'enterprise.sqlite3')
    kb = HybridKnowledge(store)
    model = client_with()
    agent = DeskAgent(store, kb, model)
    actors = {'employee': USER, 'operator': SUPPORT,
              'colleague': Principal('demo', 'colleague'),
              'second-operator': Principal('demo', 'second-operator', 'support'),
              'foreign': Principal('foreign', SUPPORT.user, 'support')}
    accounts = {hashlib.sha256(token.encode()).hexdigest(): vars(p) for token, p in actors.items()}
    with TestClient(create_app(store, kb, agent, accounts)) as http:
        yield store, kb, model, http, actors
    store.close()


def headers(token):
    return {'Authorization': 'Bearer '+token}


def test_employee_cannot_reach_management_apis_by_bypassing_navigation(workspace):
    _, _, model, http, _ = workspace
    paths = ('/system', '/mcp', '/integrations/mcp', '/integrations/embedding', '/metrics',
             '/knowledge/publications', '/evaluation/candidates', '/eval/datasets', '/eval/retention')
    for path in paths:
        assert http.get(path).status_code == 401, path
        assert http.get(path, headers=headers('employee')).status_code == 403, path
    assert http.post('/skills/reload', headers=headers('employee')).status_code == 403
    assert http.post('/eval/run', headers=headers('employee')).status_code == 403
    assert http.get('/mcp', headers=headers('operator')).status_code == 200
    model.messages.create.assert_not_called()


def test_feedback_does_not_disclose_or_promote_another_owners_private_chat(workspace):
    store, kb, model, http, actors = workspace
    publish_fixture(kb, SUPPORT, 'policy', 'VPN', '# VPN\n\nSynthetic checks')
    runs = {}
    for token in ('employee', 'operator', 'second-operator', 'foreign'):
        p = actors[token]
        run, _ = store.create_run(p, 'Private question by '+token, 'private-feedback')
        store.feedback(p, run['id'], 'bad', 'Private note by '+token)
        runs[token] = run['id']
    for token in ('operator', 'second-operator', 'foreign'):
        rows = http.get('/evaluation/candidates', headers=headers(token)).json()
        assert [row['run_id'] for row in rows] == [runs[token]]
    for token in ('operator', 'second-operator', 'foreign'):
        for source in runs:
            if source == token:
                continue
            path = '/evaluation/candidates/'+runs[source]+'/promote'
            assert http.post(path, headers=headers(token), json={'document':'policy','section':'VPN'}).status_code == 404
            assert http.get('/runs/'+runs[source], headers=headers(token)).status_code == 404
    assert http.post('/evaluation/candidates/'+runs['operator']+'/promote',
                     headers=headers('operator'), json={'document':'policy','section':'VPN'}).status_code == 200
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM regression_cases').fetchone()[0] == 1
    model.messages.create.assert_not_called()


def test_system_knowledge_count_is_scoped_to_current_readable_material(workspace):
    _, kb, _, http, actors = workspace
    publish_fixture(kb, SUPPORT, 'shared', 'Public', '# Public\n\nSynthetic shared policy')
    publish_fixture(kb, actors['second-operator'], 'private', 'Private', '# Private\n\nSynthetic private policy', visibility='private')
    publish_fixture(kb, actors['foreign'], 'foreign', 'Foreign', '# Foreign\n\nSynthetic foreign policy')
    result = http.get('/system', headers=headers('operator'))
    assert result.status_code == 200
    assert result.json()['retrieval']['chunks'] == kb.stats(SUPPORT)['total_chunks'] == 1


def test_repair_actions_match_the_assigned_operator(workspace):
    store, _, _, _, actors = workspace
    service = BusinessService(store)
    service.seed_demo()
    ticket = submit(store, REPAIR)
    case = service.get(USER, ticket['id'])
    case = action(service, SUPPORT, case, 'triage')
    case = action(service, SUPPORT, case, 'assign', assignee=SUPPORT.user)
    other = actors['second-operator']
    assert 'start' not in service.get(other, ticket['id'])['allowed_actions']
    case = action(service, SUPPORT, case, 'start')
    assert 'resolve' not in service.get(other, ticket['id'])['allowed_actions']
    with pytest.raises(Conflict):
        # Test the store-side authorization even when the caller bypasses UI actions.
        service.transition(other, ticket['id'], 'resolve', case['version'], 'forged-resolve', 'Synthetic attempt')
