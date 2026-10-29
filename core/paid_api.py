"""Legacy development-only budget gate for reproducing frozen experiments.

Rates use the published peak, cache-miss price (verified 2026-10-26).
Reservations are not refunded on failed/unknown requests: a provider may have
billed them. This ledger is a project spending guard, not a provider invoice.
Production entry points use core.model_client and never import this gate.
"""
from __future__ import annotations

from contextlib import contextmanager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
import os
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import urlsplit
import uuid


class PaidCallBlocked(RuntimeError):
    """The request was rejected before it could reach the paid API."""


def _decimal(value: Any) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise PaidCallBlocked("Invalid budget or price") from exc
    if not number.is_finite() or number < 0:
        raise PaidCallBlocked("Budget and prices must be finite and nonnegative")
    return number


def _units(cny: Decimal) -> int:
    return int((cny * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class BudgetPolicy:
    total_cny: Decimal = Decimal("20")
    reserve_cny: Decimal = Decimal("2")
    input_per_million: Decimal = Decimal("2")
    output_per_million: Decimal = Decimal("8")
    max_output_tokens: int = 2048
    max_input_bytes: int = 65536

    def __post_init__(self):
        for name in ("total_cny", "reserve_cny", "input_per_million", "output_per_million"):
            object.__setattr__(self, name, _decimal(getattr(self, name)))
        if not 0 < self.total_cny <= 20:
            raise PaidCallBlocked("This project is authorized for at most CNY 20")
        if not self.total_cny / 10 <= self.reserve_cny < self.total_cny:
            raise PaidCallBlocked("Keep at least 10% of the budget as a safety reserve")
        if self.input_per_million < 2 or self.output_per_million < 8:
            raise PaidCallBlocked("Rates must cover the verified peak cache-miss tariff")
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 4096:
            raise PaidCallBlocked("Invalid output-token limit")
        if type(self.max_input_bytes) is not int or not 1 <= self.max_input_bytes <= 131072:
            raise PaidCallBlocked("Invalid input-byte limit")

    @classmethod
    def from_environment(cls):
        try:
            return cls(
                total_cny=os.getenv("RESOLVEDESK_TOTAL_BUDGET_CNY", "20"),
                reserve_cny=os.getenv("RESOLVEDESK_RESERVE_CNY", "2"),
                input_per_million=os.getenv("RESOLVEDESK_INPUT_CNY_PER_MILLION", "2"),
                output_per_million=os.getenv("RESOLVEDESK_OUTPUT_CNY_PER_MILLION", "8"),
                max_output_tokens=int(os.getenv("RESOLVEDESK_MAX_OUTPUT_TOKENS", "2048")),
                max_input_bytes=int(os.getenv("RESOLVEDESK_MAX_INPUT_BYTES", "65536")),
            )
        except ValueError as exc:
            raise PaidCallBlocked("Invalid integer budget setting") from exc

    @property
    def operational_units(self):
        return _units(self.total_cny - self.reserve_cny)

    def cost_units(self, input_tokens: int, output_tokens: int):
        if any(type(n) is not int or n < 0 for n in (input_tokens, output_tokens)):
            raise PaidCallBlocked("Invalid token counts")
        # A CNY-per-million rate numerically equals micro-CNY per token.
        cost = self.input_per_million * input_tokens + self.output_per_million * output_tokens
        return int(cost.to_integral_value(rounding=ROUND_CEILING))


class CostLedger:
    def __init__(self, path: Path, policy: BudgetPolicy):
        self.path, self.policy = Path(path).absolute(), policy
        for ancestor in (self.path, *self.path.parents):
            if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
                raise PaidCallBlocked("A budget ledger must not follow filesystem links")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS paid_calls (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, reserved INTEGER NOT NULL,
                charged INTEGER, input_tokens INTEGER, output_tokens INTEGER,
                created_at TEXT NOT NULL)""")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @staticmethod
    def _committed(db):
        return db.execute("SELECT COALESCE(SUM(COALESCE(charged,reserved)),0) FROM paid_calls").fetchone()[0]

    def reserve(self, input_tokens: int, output_tokens: int) -> str:
        amount = self.policy.cost_units(input_tokens, output_tokens)
        call_id = uuid.uuid4().hex
        with self._transaction() as db:
            if self._committed(db) + amount > self.policy.operational_units:
                raise PaidCallBlocked("API budget exhausted; no paid request was sent")
            db.execute("INSERT INTO paid_calls VALUES (?,?,?,NULL,NULL,NULL,?)",
                       (call_id, "reserved", amount, datetime.now(timezone.utc).isoformat()))
        return call_id

    def uncertain(self, call_id):
        with self._transaction() as db:
            db.execute("UPDATE paid_calls SET status='uncertain' WHERE id=? AND status='reserved'", (call_id,))

    def settle(self, call_id: str, input_tokens: int, output_tokens: int):
        amount = self.policy.cost_units(input_tokens, output_tokens)
        with self._transaction() as db:
            row = db.execute("SELECT status,reserved FROM paid_calls WHERE id=?", (call_id,)).fetchone()
            if row is None or row[0] != "reserved":
                raise PaidCallBlocked("Unknown or already settled reservation")
            # Unexpected usage is charged conservatively and blocks future calls if necessary.
            db.execute("UPDATE paid_calls SET status=?,charged=?,input_tokens=?,output_tokens=? WHERE id=?",
                       ("settled" if amount <= row[1] else "overrun", amount, input_tokens, output_tokens, call_id))

    def summary(self):
        with self._transaction() as db:
            committed = self._committed(db)
            counts = dict(db.execute("SELECT status,COUNT(*) FROM paid_calls GROUP BY status"))
            usage = db.execute("SELECT COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0),COALESCE(SUM(CASE WHEN charged IS NULL THEN reserved ELSE 0 END),0) FROM paid_calls").fetchone()
        return {
            "budget_cny": str(self.policy.total_cny),
            "safety_reserve_cny": str(self.policy.reserve_cny),
            "conservative_commitment_cny": str(Decimal(committed) / 1_000_000),
            "available_cny": str(Decimal(max(0, self.policy.operational_units - committed)) / 1_000_000),
            "calls": counts,
            "usage_input_tokens": int(usage[0]),
            "usage_output_tokens": int(usage[1]),
            "unresolved_reservation_cny": str(Decimal(usage[2]) / 1_000_000),
        }


def _json_value(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    raise TypeError(f"Unsupported request value: {type(value).__name__}")


def _sdk_client(**kwargs):
    from anthropic import AsyncAnthropic
    return AsyncAnthropic(**kwargs)


class _GuardedMessages:
    def __init__(self, owner):
        self.owner = owner

    def _prepare(self, kwargs):
        owner = self.owner
        if os.getenv("RESOLVEDESK_PAID_API_ENABLED", "false").lower() != "true":
            raise PaidCallBlocked("Paid API calls are disabled")
        if os.getenv("RESOLVEDESK_OWN_KEY_CONFIRMED", "false").lower() != "true":
            raise PaidCallBlocked("Configure and confirm your own DeepSeek API key first")
        if not owner._key or kwargs.get("model") != "deepseek-flash":
            raise PaidCallBlocked("Only your configured deepseek-flash account is allowed")
        url = urlsplit(str(owner.base_url))
        if (url.scheme != "https" or url.hostname != "api.deepseek.com" or url.port not in (None, 443)
                or url.path.rstrip("/") != "/anthropic" or url.username or url.password or url.query or url.fragment):
            raise PaidCallBlocked("Only the official DeepSeek Anthropic endpoint is enabled")
        if kwargs.get("stream") or any(tool.get("type", "custom") != "custom" for tool in kwargs.get("tools", [])):
            raise PaidCallBlocked("Streaming and provider-hosted paid tools are not enabled yet")
        payload = dict(kwargs)
        requested_output = payload.get("max_tokens", 512)
        if type(requested_output) is not int or requested_output < 1:
            raise PaidCallBlocked("max_tokens must be a positive integer")
        payload["max_tokens"] = min(requested_output, owner.policy.max_output_tokens)
        # Do not let an optional thinking mode consume an unplanned reasoning budget.
        payload.pop("thinking", None)
        extra = dict(payload.get("extra_body") or {})
        if extra.keys() - {"thinking"}:
            raise PaidCallBlocked("Unmetered extra request fields are disabled")
        extra["thinking"] = {"type": "disabled"}
        payload["extra_body"] = extra
        encoded = json.dumps(payload, ensure_ascii=False, default=_json_value).encode("utf-8")
        if len(encoded) > owner.policy.max_input_bytes:
            raise PaidCallBlocked("Input exceeds the project's configured byte budget")
        ledger = owner.ledger()
        # UTF-8 bytes plus protocol allowance deliberately overestimate text tokens.
        call_id = ledger.reserve(len(encoded) + 4096, payload["max_tokens"])
        return payload,ledger,call_id

    async def create(self, **kwargs):
        owner = self.owner
        payload,ledger,call_id = self._prepare(kwargs)
        try:
            if owner._client is None:
                owner._client = _sdk_client(**owner._options)
            response = await owner._client.messages.create(**payload)
            usage = getattr(response, "usage", None)
            counts = [getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)]
            cached = [getattr(usage, name, 0) or 0 for name in ("cache_read_input_tokens", "cache_creation_input_tokens")]
            if all(type(n) is int and n >= 0 for n in counts + cached):
                ledger.settle(call_id, counts[0] + sum(cached), counts[1])
            else:
                ledger.uncertain(call_id)
            return response
        except BaseException:
            ledger.uncertain(call_id)
            raise

    @asynccontextmanager
    async def stream(self, **kwargs):
        owner = self.owner
        payload,ledger,call_id = self._prepare(kwargs)
        try:
            if owner._client is None:
                owner._client = _sdk_client(**owner._options)
            async with owner._client.messages.stream(**payload) as stream:
                yield stream
                response = await stream.get_final_message()
            usage = getattr(response,"usage",None)
            counts = [getattr(usage,"input_tokens",None),getattr(usage,"output_tokens",None)]
            cached = [getattr(usage,n,0) or 0 for n in ("cache_read_input_tokens","cache_creation_input_tokens")]
            if all(type(n)is int and n>=0 for n in counts+cached):
                ledger.settle(call_id,counts[0]+sum(cached),counts[1])
            else:
                ledger.uncertain(call_id)
        except BaseException:
            ledger.uncertain(call_id)
            raise


class BudgetedAnthropic:
    """Narrow adapter for the original application's messages.create/close calls."""
    def __init__(self, *, api_key: str, base_url=None, **kwargs):
        self._key = api_key
        self.base_url = base_url or "https://api.deepseek.com/anthropic"
        self.policy = BudgetPolicy.from_environment()
        self._client = None
        self._ledger = None
        self._options = dict(kwargs, api_key=api_key, base_url=self.base_url, max_retries=0)
        self.messages = _GuardedMessages(self)

    def ledger(self):
        if self._ledger is None:
            from core.financial_ledger import PostgresCostLedger
            from resolvedesk.settings import ServiceSettings
            # Shared by every main/specialist/memory/evaluation client. Never
            # rewind the budget by opening another local SQLite file.
            self._ledger = PostgresCostLedger(ServiceSettings.load().database_url,self.policy)
        return self._ledger

    async def close(self):
        if self._client is not None:
            await self._client.close()
        if self._ledger is not None and hasattr(self._ledger,"close"):
            self._ledger.close()
