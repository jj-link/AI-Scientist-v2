"""Chat Completions facade over the account-authenticated Codex Responses API.

Only caller-declared function tools are sent; this module never executes tools.
Codex manages sampling/output budgets: max_tokens, max_completion_tokens and
 temperature are accepted for existing callers but deliberately NOT transmitted.
Configuration must reject explicit overrides for these settings. Other unsupported
nondefault options fail rather than pretending to honor them. n is implemented
as independent sequential responses, with summed usage. No automatic retries.

Protocol references:
https://unpkg.com/@mariozechner/pi-ai@0.73.1/dist/providers/openai-codex-responses.js
https://github.com/openai/codex/blob/main/codex-rs/codex-api/src/endpoint/models.rs
https://github.com/openai/codex/blob/main/codex-rs/model-provider-info/src/lib.rs
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from types import SimpleNamespace
from typing import Any

import httpx
import openai
from openai.types.chat import ChatCompletion

from ai_scientist.codex_auth import CodexAuthError, get_auth

CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
# Codex gates catalog entries by this negotiated client version.
_CLIENT_VERSION = "0.153.4"


def _request(path="responses", method="POST"):
    # Exceptions must not retain bearer headers, prompt bodies or upstream data.
    return httpx.Request(method, f"{CODEX_BASE_URL}/{path}")


def _error(message):
    return openai.APIError(message, _request(), body=None)


def _bad(message="Unsupported or invalid Codex completion parameter."):
    return openai.BadRequestError(
        message, response=httpx.Response(400, request=_request()), body=None
    )


def _status(response):
    if response.is_success:
        return
    status = response.status_code
    cls = {
        400: openai.BadRequestError, 401: openai.AuthenticationError,
        403: openai.PermissionDeniedError, 404: openai.NotFoundError,
        409: openai.ConflictError, 422: openai.UnprocessableEntityError,
        429: openai.RateLimitError,
    }.get(status, openai.InternalServerError if status >= 500 else openai.APIStatusError)
    safe = httpx.Response(status, request=_request(
        "models" if response.request.method == "GET" else "responses",
        response.request.method,
    ))
    raise cls(f"Codex request failed (HTTP {status}).", response=safe, body=None)


def _headers(auth, *, stream=True):
    try:
        credentials = auth.credentials()
        token, account = credentials["access_token"], credentials["account_id"]
        if not all(isinstance(v, str) and v and v.isascii() and not any(
            ord(c) < 32 or ord(c) == 127 for c in v
        ) for v in (token, account)):
            raise ValueError
    except (CodexAuthError, KeyError, TypeError, ValueError):
        raise openai.AuthenticationError(
            "Codex login is unavailable. Connect your account in Studio.",
            response=httpx.Response(401, request=_request()), body=None,
        ) from None
    return {
        "Authorization": f"Bearer {token}", "ChatGPT-Account-Id": account,
        "originator": "ai-scientist", "OpenAI-Beta": "responses=experimental",
        "User-Agent": f"ai-scientist/{_CLIENT_VERSION}",
        "Accept": "text/event-stream" if stream else "application/json",
    }


def _seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise _bad("Codex timeout must be a finite positive number of seconds.")
    return float(value)


def _text(value):
    if not isinstance(value, str):
        raise _bad("Codex message text must be a string.")
    return value


def _parts(content, *, assistant=False):
    if content is None:
        return []
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        raise _bad("Codex messages require text or content parts.")
    result = []
    for part in content:
        if not isinstance(part, dict):
            raise _bad()
        if part.get("type") == "text":
            item = {"type": "output_text" if assistant else "input_text", "text": _text(part.get("text"))}
            if assistant:
                item["annotations"] = []
            result.append(item)
        elif part.get("type") == "image_url" and not assistant:
            image = part.get("image_url")
            if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                raise _bad("Codex image content requires an image URL.")
            url = image["url"]
            detail = image.get("detail", "auto")
            if not url.startswith(("https://", "http://", "data:image/")) or detail not in ("auto", "low", "high", "original"):
                raise _bad("Codex image URL or detail is unsupported.")
            result.append({"type": "input_image", "image_url": url, "detail": detail})
        else:
            raise _bad("Codex supports text and user/tool image content only.")
    return result


def _messages(messages):
    if not isinstance(messages, (list, tuple)) or not messages:
        raise _bad("Codex requires a nonempty message history.")
    instructions, items = [], []
    for message in messages:
        if not isinstance(message, dict):
            raise _bad()
        role = message.get("role")
        if role in ("system", "developer"):
            parts = _parts(message.get("content"))
            if any(p["type"] != "input_text" for p in parts):
                raise _bad("Codex system instructions must be text.")
            instructions.append("\n".join(p["text"] for p in parts))
        elif role in ("user", "assistant"):
            parts = _parts(message.get("content"), assistant=role == "assistant")
            if parts:
                item = {"role": role, "content": parts}
                if role == "assistant":
                    item.update(type="message", status="completed")
                items.append(item)
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list) or (calls and role != "assistant"):
                raise _bad()
            for call in calls:
                if not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("function"), dict):
                    raise _bad("Codex supports function tool history only.")
                function = call["function"]
                items.append({"type": "function_call", "call_id": _text(call.get("id")),
                              "name": _text(function.get("name")), "arguments": _text(function.get("arguments"))})
        elif role == "tool":
            content = message.get("content")
            output = content if isinstance(content, str) else _parts(content)
            items.append({"type": "function_call_output", "call_id": _text(message.get("tool_call_id")), "output": output})
        else:
            raise _bad("Unsupported Codex message role.")
    return "\n\n".join(instructions) or "You are a helpful assistant.", items


def _body(kwargs):
    options = dict(kwargs)
    model = options.pop("model", None)
    if not isinstance(model, str) or not model.strip():
        raise _bad("Codex requires a model ID.")
    instructions, items = _messages(options.pop("messages", None))
    n = options.pop("n", 1)
    if type(n) is not int or n < 1:
        raise _bad("Codex n must be a positive integer.")
    for name in ("temperature", "max_tokens", "max_completion_tokens"):
        options.pop(name, None)
    defaults = {"stream": False, "stop": None, "top_p": 1, "frequency_penalty": 0,
                "presence_penalty": 0, "logprobs": False, "top_logprobs": 0,
                "seed": None, "store": False, "logit_bias": {}}
    for name, default in defaults.items():
        value = options.pop(name, None)
        if value is not None and value != default:
            raise _bad("Codex does not support the requested sampling, streaming or storage option.")
    body = {"model": model, "store": False, "stream": True, "instructions": instructions, "input": items}
    tools = options.pop("tools", None)
    if tools is not None:
        if not isinstance(tools, list):
            raise _bad()
        body["tools"] = []
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
                raise _bad("Codex accepts caller-declared function tools only, not built-in tools.")
            function = tool["function"]
            if set(function) - {"name", "description", "parameters", "strict"}:
                raise _bad()
            _text(function.get("name"))
            # Responses can normalize omitted strictness into a strict schema,
            # unlike Chat Completions. Preserve optional FunctionSpec arguments.
            strict = function.get("strict")
            if strict is not None and type(strict) is not bool:
                raise _bad("Codex function strictness must be a boolean.")
            body["tools"].append({"type": "function", **function, "strict": strict if strict is not None else False})
    choice = options.pop("tool_choice", None)
    if choice is not None:
        if isinstance(choice, str) and choice in ("auto", "none", "required"):
            body["tool_choice"] = choice
        elif isinstance(choice, dict) and choice.get("type") == "function" and isinstance(choice.get("function"), dict):
            body["tool_choice"] = {"type": "function", "name": _text(choice["function"].get("name"))}
        else:
            raise _bad("Unsupported Codex tool choice.")
    parallel = options.pop("parallel_tool_calls", None)
    if parallel is not None:
        if type(parallel) is not bool:
            raise _bad()
        body["parallel_tool_calls"] = parallel
    effort = options.pop("reasoning_effort", None)
    if effort is not None:
        if effort not in ("none", "minimal", "low", "medium", "high", "xhigh"):
            raise _bad("Unsupported Codex reasoning effort.")
        body["reasoning"] = {"effort": effort}
    response_format = options.pop("response_format", None)
    if response_format is not None:
        if not isinstance(response_format, dict):
            raise _bad()
        kind = response_format.get("type")
        if kind in ("text", "json_object"):
            body["text"] = {"format": {"type": kind}}
        elif kind == "json_schema" and isinstance(response_format.get("json_schema"), dict):
            schema = response_format["json_schema"]
            if set(schema) - {"name", "schema", "strict", "description"} or not isinstance(schema.get("schema"), dict):
                raise _bad("Invalid Codex JSON schema format.")
            _text(schema.get("name"))
            body["text"] = {"format": {"type": "json_schema", **schema}}
        else:
            raise _bad("Unsupported Codex response format.")
    if any(value is not None for value in options.values()):
        raise _bad()
    return body, n


class _SSE:
    """Incremental SSE framing, shared by sync and async transports."""
    def __init__(self):
        self.data = []
        self.event = None
        self.output_items = {}

    def feed(self, line):
        if line:
            if line.startswith(":"):
                return None
            field, sep, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "data" and sep:
                self.data.append(value)
            elif field == "event" and sep:
                self.event = value
            elif field not in ("id", "retry"):
                raise _error("Codex returned malformed SSE framing.")
            return None
        if not self.data:
            self.event = None
            return None
        data, event = "\n".join(self.data), self.event
        self.data, self.event = [], None
        if data == "[DONE]":
            raise _error("Codex stream ended without a terminal response.")
        try:
            payload = json.loads(data)
        except (ValueError, TypeError):
            raise _error("Codex returned malformed SSE data.") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            raise _error("Codex returned an invalid stream event.")
        kind = payload["type"]
        if event and event != kind:
            raise _error("Codex returned inconsistent stream event types.")
        if kind in ("error", "response.failed"):
            raise _error("Codex response failed.")
        if kind == "response.output_item.done":
            index, item = payload.get("output_index"), payload.get("item")
            if type(index) is not int or index < 0 or not isinstance(item, dict) or index in self.output_items:
                raise _error("Codex returned an invalid completed output item.")
            self.output_items[index] = item
        if kind in ("response.done", "response.completed", "response.incomplete"):
            response = payload.get("response")
            if not isinstance(response, dict):
                raise _error("Codex terminal response is missing.")
            status = response.get("status")
            if status in ("failed", "cancelled") or response.get("error"):
                raise _error("Codex response failed or was cancelled.")
            if status not in ("completed", "incomplete"):
                raise _error("Codex returned an invalid terminal status.")
            if kind == "response.incomplete" and status != "incomplete":
                raise _error("Codex returned inconsistent terminal status.")
            # Codex can stream complete items separately and leave terminal output empty.
            if response.get("output") == [] and self.output_items:
                indexes = sorted(self.output_items)
                if indexes != list(range(len(indexes))):
                    raise _error("Codex stream is missing a completed output item.")
                response["output"] = [self.output_items[index] for index in indexes]
            return response
        return None


def _completion(responses, model):
    choices, prompt, output, cached, reasoning = [], 0, 0, 0, 0
    try:
        for index, response in enumerate(responses):
            text, refusals, calls = [], [], []
            for item in response["output"]:
                kind = item["type"]
                if kind == "reasoning":
                    continue
                if kind == "message":
                    if item.get("role") != "assistant":
                        raise ValueError
                    for part in item["content"]:
                        if part["type"] == "output_text":
                            text.append(part["text"])
                        elif part["type"] == "refusal":
                            refusals.append(part["refusal"])
                        else:
                            raise ValueError
                elif kind == "function_call":
                    if not all(isinstance(item.get(k), str) and item[k] for k in ("call_id", "name")) or not isinstance(item.get("arguments"), str):
                        raise ValueError
                    calls.append({"id": item["call_id"], "type": "function", "function": {
                        "name": item["name"], "arguments": item["arguments"]}})
                else:
                    raise ValueError
            finish = "tool_calls" if calls else "stop"
            if response["status"] == "incomplete":
                reason = (response.get("incomplete_details") or {}).get("reason")
                if reason == "max_output_tokens":
                    finish = "length"
                elif reason == "content_filter":
                    finish = "content_filter"
                else:
                    raise ValueError
            if not text and not refusals and not calls and finish == "stop":
                raise ValueError
            choices.append({"index": index, "finish_reason": finish, "message": {
                "role": "assistant", "content": "".join(text) if text else None,
                "refusal": "".join(refusals) if refusals else None, "tool_calls": calls or None}})
            usage = response.get("usage") or {}
            counts = [usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                      (usage.get("input_tokens_details") or {}).get("cached_tokens", 0),
                      (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0)]
            if any(type(value) is not int or value < 0 for value in counts):
                raise ValueError
            prompt += counts[0]
            output += counts[1]
            cached += counts[2]
            reasoning += counts[3]
        first = responses[0]
        return ChatCompletion(
            id=first["id"], object="chat.completion", created=first.get("created_at", int(time.time())),
            model=first.get("model", model), system_fingerprint=first.get("system_fingerprint"), choices=choices,
            usage={"prompt_tokens": prompt, "completion_tokens": output, "total_tokens": prompt + output,
                   "prompt_tokens_details": {"cached_tokens": cached},
                   "completion_tokens_details": {"reasoning_tokens": reasoning}},
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        raise _error("Codex returned an invalid completion payload.") from None


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise openai.APITimeoutError(_request())
    return remaining


class CodexClient:
    """Synchronous facade. Timeout bounds HTTP inactivity and checks batch elapsed time."""
    def __init__(self, timeout=600, auth=None, *, transport=None):
        self.timeout = _seconds(timeout)
        self.auth = auth if auth is not None else get_auth()
        self._http = httpx.Client(timeout=self.timeout, transport=transport, follow_redirects=False, trust_env=False)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        timeout = _seconds(kwargs.pop("timeout", self.timeout))
        body, n = _body(kwargs)
        deadline, responses = time.monotonic() + timeout, []
        try:
            for _ in range(n):
                headers = _headers(self.auth)
                with self._http.stream("POST", f"{CODEX_BASE_URL}/responses", headers=headers,
                                       json=body, timeout=_remaining(deadline)) as response:
                    _status(response)
                    # Codex may omit this header; the parser still requires a valid terminal SSE event.
                    if response.headers.get("content-type", "text/event-stream").split(";", 1)[0].strip() != "text/event-stream":
                        raise _error("Codex did not return an SSE response.")
                    parser = _SSE()
                    for line in response.iter_lines():
                        _remaining(deadline)
                        terminal = parser.feed(line)
                        if terminal is not None:
                            responses.append(terminal)
                            break
                    else:
                        raise _error("Codex stream closed before its terminal response.")
        except httpx.TimeoutException:
            raise openai.APITimeoutError(_request()) from None
        except httpx.HTTPError:
            raise openai.APIConnectionError(message="Codex connection failed.", request=_request()) from None
        return _completion(responses, body["model"])

    def close(self):
        self._http.close()


class CodexAsyncClient:
    """Async facade with cancellation-safe stream closure and a total request deadline."""
    def __init__(self, timeout=600, auth=None, *, transport=None):
        self.timeout = _seconds(timeout)
        self.auth = auth if auth is not None else get_auth()
        self._http = httpx.AsyncClient(timeout=self.timeout, transport=transport, follow_redirects=False, trust_env=False)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        timeout = _seconds(kwargs.pop("timeout", self.timeout))
        body, n = _body(kwargs)
        try:
            return await asyncio.wait_for(self._batch(body, n, timeout), timeout=timeout)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise openai.APITimeoutError(_request()) from None
        except httpx.HTTPError:
            raise openai.APIConnectionError(message="Codex connection failed.", request=_request()) from None

    async def _batch(self, body, n, timeout):
        responses = []
        for _ in range(n):
            # Refresh/keyring/file-lock operations must not block Studio's event loop.
            headers = await asyncio.to_thread(_headers, self.auth)
            async with self._http.stream("POST", f"{CODEX_BASE_URL}/responses", headers=headers,
                                         json=body, timeout=timeout) as response:
                _status(response)
                if response.headers.get("content-type", "text/event-stream").split(";", 1)[0].strip() != "text/event-stream":
                    raise _error("Codex did not return an SSE response.")
                parser = _SSE()
                async for line in response.aiter_lines():
                    terminal = parser.feed(line)
                    if terminal is not None:
                        responses.append(terminal)
                        break
                else:
                    raise _error("Codex stream closed before its terminal response.")
        return _completion(responses, body["model"])

    async def close(self):
        await self._http.aclose()


def list_models(timeout=5, auth=None, *, transport=None) -> list[str]:
    """Fetch this account's real model catalog; never substitute a static list."""
    timeout = _seconds(timeout)
    headers = _headers(auth if auth is not None else get_auth(), stream=False)
    try:
        with httpx.Client(timeout=timeout, transport=transport, follow_redirects=False, trust_env=False) as client:
            response = client.get(f"{CODEX_BASE_URL}/models", params={"client_version": _CLIENT_VERSION}, headers=headers)
            _status(response)
            try:
                data = response.json()
                models = data["models"]
                if not isinstance(models, list) or any(
                    not isinstance(item, dict) or not isinstance(item.get("slug"), str) or not item["slug"].strip()
                    for item in models
                ):
                    raise ValueError
                return list(dict.fromkeys(item["slug"] for item in models))
            except (ValueError, KeyError, TypeError):
                raise _error("Codex returned an invalid model catalog.") from None
    except httpx.TimeoutException:
        raise openai.APITimeoutError(_request("models", "GET")) from None
    except httpx.HTTPError:
        raise openai.APIConnectionError(message="Codex model discovery connection failed.", request=_request("models", "GET")) from None
