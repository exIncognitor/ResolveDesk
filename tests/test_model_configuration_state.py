import hashlib

import pytest
from fastapi.testclient import TestClient

from core.model_client import ModelClient, model_identity
from resolvedesk.agent import DeskAgent
from resolvedesk.app import create_app
from resolvedesk.eval_sandbox import freeze
from resolvedesk.eval_state import EvalState
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.store import Principal, Store
from pathlib import Path


def test_api_key_rotation_does_not_change_execution_identity():
    a = ModelClient(api_key='synthetic-old-key', model='configured')
    b = ModelClient(api_key='synthetic-new-key', model='configured')
    assert model_identity(a) == model_identity(b)
    assert 'synthetic' not in str(model_identity(a))


def test_evaluation_snapshot_rejects_a_different_model_profile(tmp_path):
    store = Store(tmp_path/'configuration.sqlite3')
    HybridKnowledge(store)
    state = EvalState(store, 'model-change')
    state.model_identity = {'protocol': 'openai', 'model': 'model-a', 'base_url': 'https://models.example/v1'}
    principal = Principal('demo', 'support', 'support')
    root = Path(__file__).resolve().parents[1]
    try:
        snapshot = freeze(state, principal, {'assets': []}, root)
        assert snapshot['model_identity']['model'] == 'model-a'
        state.model_identity = dict(state.model_identity, model='model-b')
        with pytest.raises(ValueError, match='model configuration changed'):
            freeze(state, principal, {'assets': []}, root)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_conversation_checkpoint_rejects_a_different_model_before_calling_provider(tmp_path):
    store = Store(tmp_path/'conversation.sqlite3')
    knowledge = HybridKnowledge(store)
    client = ModelClient(api_key='offline', model='model-b')
    agent = DeskAgent(store, knowledge, client)
    principal = Principal('demo','learner')
    run, _ = store.create_run(principal, 'hello', 'model-checkpoint')
    store.checkpoint(principal, run['id'], {'model_identity': dict(model_identity(client), model='model-a'), 'sources': {}})
    try:
        with pytest.raises(ValueError, match='Model configuration changed'):
            await agent._loop(principal, run['id'])
        assert client._client is None
    finally:
        await agent.close()
        store.close()


def test_support_configuration_is_redacted_and_employee_is_denied(tmp_path):
    store = Store(tmp_path/'api.sqlite3')
    model = ModelClient(api_key='never-return-this-key', model='configured-model', protocol='openai',
                        base_url='https://model.example/v1')
    knowledge = HybridKnowledge(store)
    agent = DeskAgent(store, knowledge, model)
    actors = {'employee': Principal('demo','employee'), 'operator': Principal('demo','operator','support')}
    accounts = {hashlib.sha256(token.encode()).hexdigest(): vars(actor) for token, actor in actors.items()}
    try:
        with TestClient(create_app(store, knowledge, agent, accounts)) as api:
            assert api.get('/system',headers={'Authorization':'Bearer employee'}).status_code == 403
            result = api.get('/system',headers={'Authorization':'Bearer operator'})
            assert result.status_code == 200
            data = result.json()
            assert data['model']['model'] == 'configured-model'
            assert data['model']['configured'] is True
            assert 'budget' not in data and 'usage' in data
            assert 'never-return-this-key' not in result.text
            assert api.get('/me',headers={'Authorization':'Bearer employee'}).json()['role'] == 'user'
    finally:
        store.close()

