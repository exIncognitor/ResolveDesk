"""All pytest runs are offline with respect to paid model APIs."""
import pytest


@pytest.fixture(autouse=True)
def paid_calls_disabled_by_default(monkeypatch):
    # Gateway unit tests explicitly opt in with fake HTTP/SDK transports.
    monkeypatch.setenv("RESOLVEDESK_PAID_API_ENABLED", "false")
