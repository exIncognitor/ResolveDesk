"""Configured model transport and token telemetry; no spending limits or ledger."""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid


def _sdk_client(**kwargs):
    from anthropic import AsyncAnthropic
    return AsyncAnthropic(**kwargs)


def model_identity(client):
    if isinstance(client, ModelClient):
        return {key: getattr(client, key) for key in ('protocol', 'model', 'base_url')}
    value = getattr(client, 'model', None)
    return {'protocol': 'injected', 'model': value if isinstance(value, str) else type(client).__name__}


def _value(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        return {key: _value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    return value


def _text(content):
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    blocks = _value(content)
    if any(block.get("type") != "text" for block in blocks):
        raise ValueError("This model adapter accepts text and function tools only")
    return "\n".join(block.get("text", "") for block in blocks)


def _chat_payload(kwargs, model, stream=False):
    """Translate the application's Messages contract into Chat Completions."""
    data = dict(kwargs)
    messages = []
    if data.get("system"):
        messages.append({"role": "system", "content": _text(data["system"])})
    for message in data.get("messages", []):
        role, content = message["role"], _value(message.get("content", ""))
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        text, calls, results = [], [], []
        for block in content:
            kind = block.get("type")
            if kind == "text":
                text.append(block.get("text", ""))
            elif kind == "tool_use" and role == "assistant":
                calls.append({"id": block["id"], "type": "function",
                              "function": {"name": block["name"], "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)}})
            elif kind == "tool_result" and role == "user":
                results.append({"role": "tool", "tool_call_id": block["tool_use_id"],
                                "content": ("Tool error: " if block.get("is_error") else "") + _text(block.get("content", ""))})
            else:
                raise ValueError("Unsupported message content for Chat Completions")
        messages.extend(results)
        if text or calls:
            item = {"role": role, "content": "\n".join(text) or None}
            if calls:
                item["tool_calls"] = calls
            messages.append(item)
    payload = {"model": model, "messages": messages}
    for name in ("max_tokens", "temperature", "top_p"):
        if name in data:
            payload[name] = data[name]
    if data.get("stop_sequences"):
        payload["stop"] = data["stop_sequences"]
    if data.get("tools"):
        payload["tools"] = [{"type": "function", "function": {"name": tool["name"],
                            "description": tool.get("description", ""), "parameters": tool["input_schema"]}}
                            for tool in data["tools"]]
        choice = data.get("tool_choice", {"type": "auto"})
        kind = choice.get("type") if isinstance(choice, dict) else choice
        payload["tool_choice"] = {"any": "required", "auto": "auto", "none": "none"}.get(kind, kind)
        if kind == "tool":
            payload["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
    if stream:
        payload.update(stream=True, stream_options={"include_usage": True})
    return payload


def _usage(raw):
    if not isinstance(raw, dict):
        return None
    input_tokens, output_tokens = raw.get("prompt_tokens"), raw.get("completion_tokens")
    if any(type(n) is not int or n < 0 for n in (input_tokens, output_tokens)):
        return None
    details = raw.get("prompt_tokens_details") or {}
    return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                           cache_read_input_tokens=details.get("cached_tokens", 0) or 0,
                           cache_creation_input_tokens=0)


def _message(model, text, calls, finish, usage, identity=""):
    blocks = [{"type": "text", "text": text}] if text else []
    for call in calls:
        args = json.loads(call["function"].get("arguments") or "{}")
        if not isinstance(args, dict) or not call.get("id") or not call["function"].get("name"):
            raise ValueError("Invalid function call returned by model")
        blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": args})
    reason = "max_tokens" if finish == "length" else "tool_use" if calls else "end_turn"
    return SimpleNamespace(id=identity, model=model, content=blocks, stop_reason=reason, usage=usage)


class _ChatStream:
    def __init__(self, response, model):
        self.response, self.model = response, model
        self.text, self.calls, self.finish, self.usage, self.identity = "", {}, None, None, ""
        self.done = False
        self._iterator = self._consume()

    @property
    def text_stream(self):
        return self._iterator

    def _event(self, raw):
        if raw.strip() == "[DONE]":
            return []
        event = json.loads(raw)
        if event.get("error"):
            raise RuntimeError("Model streaming response reported an error")
        self.identity = event.get("id") or self.identity
        if event.get("usage") is not None:
            self.usage = _usage(event["usage"])
        emitted = []
        for choice in event.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            self.finish = choice.get("finish_reason") or self.finish
            delta = choice.get("delta") or {}
            value = delta.get("content")
            if value:
                self.text += value
                emitted.append(value)
            for part in delta.get("tool_calls", []):
                target = self.calls.setdefault(part["index"], {"id": "", "function": {"name": "", "arguments": ""}})
                if part.get("id"):
                    target["id"] = part["id"]
                function = part.get("function") or {}
                for field in ("name", "arguments"):
                    if function.get(field):
                        target["function"][field] += function[field]
        return emitted

    async def _consume(self):
        data = []
        async for line in self.response.aiter_lines():
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                for value in self._event("\n".join(data)):
                    yield value
                data.clear()
        if data:
            for value in self._event("\n".join(data)):
                yield value
        self.done = True

    async def get_final_message(self):
        if not self.done:
            async for _ in self._iterator:
                pass
        return _message(self.model, self.text, [self.calls[i] for i in sorted(self.calls)],
                        self.finish, self.usage, self.identity)


class _Messages:
    def __init__(self, owner):
        self.owner = owner

    async def create(self, **kwargs):
        owner = self.owner
        try:
            if owner.protocol == "anthropic":
                response = await owner._sdk().messages.create(**(dict(kwargs) | {"model": owner.model}))
            else:
                result = await owner._http().post(owner.chat_url, json=_chat_payload(kwargs, owner.model),
                                                  headers=owner._headers())
                result.raise_for_status()
                data = result.json()
                choice = data["choices"][0]
                message = choice["message"]
                response = _message(owner.model, message.get("content") or "", message.get("tool_calls") or [],
                                    choice.get("finish_reason"), _usage(data.get("usage")), data.get("id", ""))
            owner._record(response)
            return response
        except BaseException:
            owner._failed += 1
            raise

    @asynccontextmanager
    async def stream(self, **kwargs):
        owner = self.owner
        try:
            if owner.protocol == "anthropic":
                async with owner._sdk().messages.stream(**(dict(kwargs) | {"model": owner.model})) as stream:
                    yield stream
                    response = await stream.get_final_message()
            else:
                async with owner._http().stream("POST", owner.chat_url, json=_chat_payload(kwargs, owner.model, True),
                                                 headers=owner._headers()) as result:
                    result.raise_for_status()
                    stream = _ChatStream(result, owner.model)
                    yield stream
                    response = await stream.get_final_message()
            owner._record(response)
        except BaseException:
            owner._failed += 1
            raise


class ModelClient:
    """One configured provider per client; API keys are never returned by describe()."""
    def __init__(self, *, api_key=None, base_url=None, model=None, protocol=None, **kwargs):
        self.protocol = (protocol or os.getenv("RESOLVEDESK_MODEL_PROTOCOL") or "anthropic").strip().lower()
        if self.protocol not in {"anthropic", "openai"}:
            raise ValueError("RESOLVEDESK_MODEL_PROTOCOL must be anthropic or openai")
        self.model = (model or os.getenv("RESOLVEDESK_MODEL") or os.getenv("ANTHROPIC_MODEL") or "deepseek-flash").strip()
        self._key = api_key or os.getenv("RESOLVEDESK_MODEL_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or ""
        default_url = "https://api.deepseek.com/anthropic" if self.protocol == "anthropic" else "https://api.deepseek.com/v1"
        old_url = os.getenv("ANTHROPIC_BASE_URL") if self.protocol == "anthropic" else None
        self.base_url = str(base_url or os.getenv("RESOLVEDESK_MODEL_BASE_URL") or old_url or default_url).rstrip("/")
        parsed = urlsplit(self.base_url)
        if (not self.model or parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Invalid server-side model configuration")
        self.chat_url = self.base_url if self.base_url.endswith("/chat/completions") else self.base_url + "/chat/completions"
        self._client = None
        self._http_client = kwargs.get("http_client") if self.protocol == "openai" else None
        self._timeout = kwargs.get("timeout", 25)
        self._options = dict(kwargs, api_key=self._key, base_url=self.base_url, max_retries=0)
        self._calls = self._failed = self._usage_missing = self._input = self._output = self._cached = 0
        self.worker = uuid.uuid4().hex[:12]
        self.messages = _Messages(self)

    def _sdk(self):
        if self._client is None:
            self._client = _sdk_client(**self._options)
        return self._client

    def _http(self):
        if self._http_client is None:
            import httpx
            self._http_client = httpx.AsyncClient(timeout=self._timeout)
        return self._http_client

    def _headers(self):
        return {"Authorization": "Bearer " + self._key, "Content-Type": "application/json"}

    def _record(self, response):
        self._calls += 1
        usage = getattr(response, "usage", None)
        counts = [getattr(usage, key, None) for key in ("input_tokens", "output_tokens")]
        if any(type(n) is not int or n < 0 for n in counts):
            self._usage_missing += 1
            return
        self._input += counts[0]
        self._output += counts[1]
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        if type(cached) is int and cached >= 0:
            self._cached += cached

    def describe(self):
        return {"protocol": self.protocol, "model": self.model, "base_url": self.base_url, "configured": bool(self._key)}

    def usage_summary(self):
        return {"scope": "current_client_worker", "worker": self.worker, "completed_calls": self._calls,
                "failed_or_cancelled_calls": self._failed, "usage_unavailable_calls": self._usage_missing,
                "input_tokens": self._input, "output_tokens": self._output, "cache_read_input_tokens": self._cached}

    async def close(self):
        if self._client is not None:
            await self._client.close()
        if self._http_client is not None:
            await self._http_client.aclose()

