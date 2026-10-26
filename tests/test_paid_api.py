import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from core.paid_api import BudgetedAnthropic, BudgetPolicy, CostLedger, PaidCallBlocked


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".codex-tmp" / ("budget-tests-" + uuid.uuid4().hex)


class LedgerTests(unittest.TestCase):
    def ledger(self, policy=None):
        return CostLedger(SCRATCH / (uuid.uuid4().hex + ".sqlite3"), policy or BudgetPolicy())

    def test_rejects_unauthorized_budget_and_invalid_rates(self):
        for kwargs in ({"total_cny": "21"}, {"total_cny": "NaN"}, {"reserve_cny": "0"},
                       {"input_per_million": "0"}, {"output_per_million": "7"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(PaidCallBlocked):
                BudgetPolicy(**kwargs)

    def test_reservation_is_persistent_and_blocks_before_overspend(self):
        ledger = self.ledger()
        ledger.reserve(1_000_000, 2_000_000)  # CNY 18 operational allowance
        again = CostLedger(ledger.path, BudgetPolicy())
        self.assertEqual(again.summary()["available_cny"], "0")
        with self.assertRaises(PaidCallBlocked):
            again.reserve(1, 0)

    def test_success_releases_only_the_unused_reservation(self):
        ledger = self.ledger()
        call = ledger.reserve(1000, 1000)
        ledger.settle(call, 100, 50)
        self.assertEqual(Decimal(ledger.summary()["conservative_commitment_cny"]), Decimal("0.0006"))
        with self.assertRaises(PaidCallBlocked):
            ledger.settle(call, 0, 0)

    def test_unknown_calls_keep_the_reservation(self):
        ledger = self.ledger()
        call = ledger.reserve(1000, 1000)
        ledger.uncertain(call)
        self.assertEqual(ledger.summary()["conservative_commitment_cny"], "0.01")
        self.assertEqual(ledger.summary()["calls"], {"uncertain": 1})

    def test_independent_connections_cannot_over_reserve(self):
        ledger = self.ledger()
        def attempt(_):
            try:
                CostLedger(ledger.path, BudgetPolicy()).reserve(500_000, 0)
                return 1
            except PaidCallBlocked:
                return 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(attempt, range(30))), 18)
        self.assertEqual(ledger.summary()["available_cny"], "0")


class GatewayTests(unittest.IsolatedAsyncioTestCase):
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
        self.client = BudgetedAnthropic(api_key="test-only-not-a-real-key")
        self.client._ledger = CostLedger(SCRATCH / (uuid.uuid4().hex + ".sqlite3"), self.client.policy)
        self.response = SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=50), content=[])
        self.fake = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=self.response)), close=AsyncMock())
        self.factory = patch("core.paid_api._sdk_client", return_value=self.fake)
        self.factory_mock = self.factory.start()
        self.addCleanup(self.factory.stop)
        self.request = dict(model="deepseek-flash", max_tokens=512,
                            messages=[{"role": "user", "content": "测试"}])

    async def test_disabled_switch_blocks_without_constructing_sdk(self):
        os.environ["RESOLVEDESK_PAID_API_ENABLED"] = "false"
        with self.assertRaises(PaidCallBlocked):
            await self.client.messages.create(**self.request)
        self.factory_mock.assert_not_called()

    async def test_unconfirmed_key_blocks(self):
        os.environ["RESOLVEDESK_OWN_KEY_CONFIRMED"] = "false"
        with self.assertRaises(PaidCallBlocked):
            await self.client.messages.create(**self.request)
        self.factory_mock.assert_not_called()

    async def test_expensive_model_and_foreign_endpoint_block(self):
        for model in ("deepseek-v4-pro", "claude-opus", "deepseek-chat"):
            with self.subTest(model=model), self.assertRaises(PaidCallBlocked):
                await self.client.messages.create(**dict(self.request, model=model))
        self.client.base_url = "https://other.example/anthropic"
        with self.assertRaises(PaidCallBlocked):
            await self.client.messages.create(**self.request)
        self.factory_mock.assert_not_called()

    async def test_budget_exhausted_blocks_before_network(self):
        self.client._ledger.reserve(1_000_000, 2_000_000)
        with self.assertRaises(PaidCallBlocked):
            await self.client.messages.create(**self.request)
        self.factory_mock.assert_not_called()

    async def test_sdk_retries_disabled_and_thinking_off(self):
        response = await self.client.messages.create(**dict(self.request, max_tokens=9000,
                                                            thinking={"type": "enabled", "budget_tokens": 8000}))
        self.assertIs(response, self.response)
        self.assertEqual(self.factory_mock.call_args.kwargs["max_retries"], 0)
        sent = self.fake.messages.create.call_args.kwargs
        self.assertEqual(sent["max_tokens"], 2048)
        self.assertNotIn("thinking", sent)
        self.assertEqual(sent["extra_body"]["thinking"], {"type": "disabled"})
        self.assertEqual(self.client._ledger.summary()["calls"], {"settled": 1})

    async def test_failed_or_cancelled_calls_are_not_refunded(self):
        for error in (TimeoutError(), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                self.fake.messages.create.side_effect = error
                with self.assertRaises(type(error)):
                    await self.client.messages.create(**self.request)
        self.assertEqual(self.client._ledger.summary()["calls"], {"uncertain": 2})

    async def test_missing_usage_keeps_cost_reservation(self):
        self.fake.messages.create.return_value = SimpleNamespace(content=[])
        await self.client.messages.create(**self.request)
        self.assertEqual(self.client._ledger.summary()["calls"], {"uncertain": 1})

    async def test_cached_tokens_are_conservatively_charged(self):
        self.fake.messages.create.return_value = SimpleNamespace(usage=SimpleNamespace(
            input_tokens=100, output_tokens=50, cache_read_input_tokens=200, cache_creation_input_tokens=0))
        await self.client.messages.create(**self.request)
        self.assertEqual(Decimal(self.client._ledger.summary()["conservative_commitment_cny"]), Decimal("0.001"))

    async def test_streaming_builtin_tools_and_oversized_input_block(self):
        for extra in ({"stream": True}, {"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
                      {"extra_body": {"unknown_billing_option": True}},
                      {"messages": [{"role": "user", "content": "x" * 70000}]}):
            with self.subTest(extra=list(extra)), self.assertRaises(PaidCallBlocked):
                await self.client.messages.create(**dict(self.request, **extra))
        self.factory_mock.assert_not_called()


class WiringTests(unittest.TestCase):
    def test_all_application_anthropic_clients_use_the_gate(self):
        expected = {"agents/agent_orchestrator.py", "core/intent_recognizer.py", "tooling/tool_manager.py",
                    "memory/conversation_memory.py", "evaluation/evaluator.py"}
        found = set()
        for directory in ("agents", "api", "core", "tooling", "memory", "evaluation", "monitor"):
            for path in (ROOT / directory).glob("*.py"):
                relative = path.relative_to(ROOT).as_posix()
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module == "anthropic":
                        self.assertEqual(relative, "core/paid_api.py", relative)
                    if isinstance(node, ast.ImportFrom) and node.module == "core.paid_api":
                        if any(n.name == "BudgetedAnthropic" and n.asname == "AsyncAnthropic" for n in node.names):
                            found.add(relative)
        self.assertEqual(found, expected)


if __name__ == "__main__":
    unittest.main()
