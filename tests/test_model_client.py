"""Model adapters are exercised through in-process transports, never paid APIs."""
import asyncio
import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from core.model_client import ModelClient


def request_options():
    return dict(model="old-hardcoded-name", max_tokens=8192, temperature=0,
                system="system policy", tools=[{"name": "lookup", "description": "Read a resource",
                "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}}}],
                tool_choice={"type": "any"}, messages=[
                    {"role": "user", "content": "look up the item"},
                    {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"id": "x"}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "item found"}]},
                ])


@pytest.mark.asyncio
async def test_anthropic_configuration_stream_and_no_budget_dependency(monkeypatch):
    monkeypatch.setenv("RESOLVEDESK_MODEL", "configured-model")
    monkeypatch.setenv("RESOLVEDESK_MODEL_BASE_URL", "https://models.example/anthropic")
    monkeypatch.setenv("RESOLVEDESK_MODEL_API_KEY", "synthetic-private-key")
    monkeypatch.setenv("RESOLVEDESK_TOTAL_BUDGET_CNY", "0")
    monkeypatch.setenv("RESOLVEDESK_PAID_API_ENABLED", "false")
    monkeypatch.setenv("RESOLVEDESK_OWN_KEY_CONFIRMED", "false")
    seen = []
    async def handle(request):
        seen.append((str(request.url), json.loads(request.content), request.headers["x-api-key"]))
        return httpx.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": "configured-model",
            "content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 4},
        })
    sdk = anthropic.AsyncAnthropic(api_key="synthetic-private-key", base_url="https://models.example/anthropic",
                                  max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    monkeypatch.setattr("core.model_client._sdk_client", lambda **kwargs: sdk)
    client = ModelClient()
    try:
        response = await client.messages.create(**request_options())
        assert response.content[0].text == "done"
        assert seen[0][0] == "https://models.example/anthropic/v1/messages"
        assert seen[0][1]["model"] == "configured-model"
        assert seen[0][1]["max_tokens"] == 8192
        assert seen[0][2] == "synthetic-private-key"
        assert not hasattr(client, "ledger") and not hasattr(client, "policy")
        assert "synthetic-private-key" not in json.dumps(client.describe())
        assert client.usage_summary()["input_tokens"] == 12
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_openai_text_and_tool_roundtrip(monkeypatch):
    seen = []
    async def handle(request):
        seen.append((str(request.url), dict(request.headers), json.loads(request.content)))
        return httpx.Response(200, json={"id": "chat_1", "choices": [{"message": {"content": None, "tool_calls": [
            {"id": "call_2", "type": "function", "function": {"name": "lookup", "arguments": '{"id":"y"}'}}
        ]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 20, "completion_tokens": 6}})
    transport = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client = ModelClient(protocol="openai", api_key="synthetic-openai-key", base_url="https://model.example/v1",
                         model="my-model", http_client=transport)
    try:
        reply = await client.messages.create(**request_options())
        assert reply.stop_reason == "tool_use"
        assert reply.content == [{"type": "tool_use", "id": "call_2", "name": "lookup", "input": {"id": "y"}}]
        url, headers, body = seen[0]
        assert url == "https://model.example/v1/chat/completions"
        assert headers["authorization"] == "Bearer synthetic-openai-key"
        assert body["model"] == "my-model" and body["tool_choice"] == "required"
        assert body["messages"][0] == {"role": "system", "content": "system policy"}
        assert body["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": "item found"}
        assert json.loads(body["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {"id": "x"}
        assert client.usage_summary()["output_tokens"] == 6
    finally:
        await client.close()


def sse(events):
    return "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events) + "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_openai_stream_accumulates_unicode_tool_fragments_and_final_usage():
    events = [
        {"id": "chat_2", "choices": [{"index": 0, "delta": {"content": "你好"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_3", "function": {"name": "lookup", "arguments": '{"id":'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"z"}'}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 9, "prompt_tokens_details": {"cached_tokens": 4}}},
    ]
    seen = []
    async def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, text=sse(events), headers={"content-type": "text/event-stream"})
    client = ModelClient(protocol="openai", model="stream-model", api_key="offline",
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        async with client.messages.stream(**request_options()) as stream:
            assert "".join([value async for value in stream.text_stream]) == "你好"
            response = await stream.get_final_message()
        assert response.content[1]["input"] == {"id": "z"}
        assert response.stop_reason == "tool_use"
        assert seen[0]["stream_options"]["include_usage"] is True
        assert client.usage_summary()["cache_read_input_tokens"] == 4
        assert client.usage_summary()["completed_calls"] == 1
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_failures_do_not_retry_and_never_require_a_financial_database(streaming):
    seen = []
    async def handle(request):
        seen.append(request)
        return httpx.Response(503, json={"error": {"message": "synthetic failure"}})
    client = ModelClient(protocol="openai", model="any-compatible-model", api_key="offline",
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            if streaming:
                async with client.messages.stream(**request_options()):
                    pass
            else:
                await client.messages.create(**request_options())
        assert len(seen) == 1 and client.usage_summary()["failed_or_cancelled_calls"] == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_missing_usage_is_not_reported_as_measured_zero():
    async def handle(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]})
    client = ModelClient(protocol="openai", api_key="offline",
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        result = await client.messages.create(messages=[{"role": "user", "content": "hi"}], max_tokens=100)
        assert result.usage is None
        assert client.usage_summary()["usage_unavailable_calls"] == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_automated_tests_forbid_unmocked_model_network_calls():
    client = ModelClient(protocol="openai", api_key="offline")
    with pytest.raises(AssertionError, match="in-process"):
        await client.messages.create(messages=[{"role": "user", "content": "hi"}], max_tokens=100)
    await client.close()


@pytest.mark.parametrize("kwargs", [{"protocol": "unknown"}, {"base_url": "file:///tmp/model"},
                                   {"base_url": "https://user:secret@example.com/v1"}])
def test_invalid_configuration_is_rejected_without_exposing_credentials(kwargs):
    with pytest.raises(ValueError):
        ModelClient(**kwargs)

