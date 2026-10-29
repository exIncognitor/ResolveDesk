"""All pytest runs are offline with respect to paid model APIs."""
import pytest


@pytest.fixture(autouse=True)
def paid_calls_disabled_by_default(monkeypatch):
    # Gateway unit tests explicitly opt in with fake HTTP/SDK transports.
    monkeypatch.setenv("RESOLVEDESK_PAID_API_ENABLED", "false")
    import core.model_client as models
    def blocked_sdk(**kwargs):
        raise AssertionError('Tests must inject a fake SDK or an in-process model transport')
    monkeypatch.setattr(models, '_sdk_client', blocked_sdk)
    original_http = models.ModelClient._http
    def blocked_http(client):
        if client._http_client is None:
            raise AssertionError('Tests must inject an in-process model HTTP transport')
        return original_http(client)
    monkeypatch.setattr(models.ModelClient, '_http', blocked_http)
