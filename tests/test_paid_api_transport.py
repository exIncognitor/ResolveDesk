"""Exercise the real SDK against an in-process HTTP transport, never the network."""
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from core.paid_api import BudgetedAnthropic, CostLedger


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".codex-tmp" / ("budget-tests-" + uuid.uuid4().hex)
HAS_SDK = importlib.util.find_spec("anthropic") is not None and importlib.util.find_spec("httpx") is not None


@unittest.skipUnless(HAS_SDK, "Optional SDK transport check needs anthropic and httpx")
class SDKTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {
            "RESOLVEDESK_PAID_API_ENABLED": "true",
            "RESOLVEDESK_OWN_KEY_CONFIRMED": "true",
            "RESOLVEDESK_TOTAL_BUDGET_CNY": "20",
            "RESOLVEDESK_RESERVE_CNY": "2",
            "RESOLVEDESK_INPUT_CNY_PER_MILLION": "2",
            "RESOLVEDESK_OUTPUT_CNY_PER_MILLION": "8",
            "RESOLVEDESK_MAX_OUTPUT_TOKENS": "2048",
            "RESOLVEDESK_MAX_INPUT_BYTES": "65536",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.requests = []
        self.client = BudgetedAnthropic(api_key="unit-test-only-no-real-secret")
        self.client._ledger = CostLedger(SCRATCH / (uuid.uuid4().hex + ".sqlite3"), self.client.policy)

    async def run_with_transport(self, status):
        import anthropic
        import httpx

        async def handler(request):
            self.requests.append((str(request.url), json.loads(request.content)))
            if status != 200:
                return httpx.Response(status, json={"type": "error", "error": {
                    "type": "overloaded_error", "message": "offline synthetic failure"}})
            return httpx.Response(200, json={
                "id": "msg_offline", "type": "message", "role": "assistant",
                "model": "deepseek-flash", "stop_reason": "end_turn", "stop_sequence": None,
                "content": [{"type": "text", "text": "offline integration response"}],
                "usage": {"input_tokens": 12, "output_tokens": 4},
            })

        sdk = anthropic.AsyncAnthropic(
            api_key="unit-test-only-no-real-secret",
            base_url="https://api.deepseek.com/anthropic", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        try:
            with patch("core.paid_api._sdk_client", return_value=sdk):
                return await self.client.messages.create(
                    model="deepseek-flash", max_tokens=128,
                    messages=[{"role": "user", "content": "offline SDK validation"}],
                )
        finally:
            await sdk.close()

    async def test_payload_and_usage_with_real_sdk(self):
        response = await self.run_with_transport(200)
        self.assertEqual(response.content[0].text, "offline integration response")
        self.assertEqual(len(self.requests), 1)
        url, body = self.requests[0]
        self.assertEqual(url, "https://api.deepseek.com/anthropic/v1/messages")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(self.client._ledger.summary()["calls"], {"settled": 1})

    async def test_provider_failure_does_not_retry_or_refund(self):
        import anthropic
        with self.assertRaises(anthropic.APIStatusError):
            await self.run_with_transport(503)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.client._ledger.summary()["calls"], {"uncertain": 1})


if __name__ == "__main__":
    unittest.main()
