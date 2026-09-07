"""Offline protocol regressions; injected transports cannot reach live accounts."""
import asyncio
import json
import unittest

import httpx
import openai

from ai_scientist.codex_provider import CODEX_BASE_URL, CodexAsyncClient, CodexClient, list_models


class Auth:
    def credentials(self):
        return {"access_token": "fixture-bearer-secret", "account_id": "fixture-account"}


def terminal(text="answer", **changes):
    response = {
        "id": "resp_fixture", "model": "account-model", "created_at": 123,
        "status": "completed", "system_fingerprint": "fixture-fingerprint",
        "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 11, "output_tokens": 7,
                  "input_tokens_details": {"cached_tokens": 3},
                  "output_tokens_details": {"reasoning_tokens": 2}},
    }
    response.update(changes)
    return response


def event(kind, response=None, **extra):
    data = {"type": kind, **extra}
    if response is not None:
        data["response"] = response
    return f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode()


def sse(data):
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)


def request_options(**changes):
    return {"model": "account-model", "messages": [{"role": "user", "content": "question"}], **changes}


class CodexProviderTests(unittest.TestCase):
    def client(self, handler):
        client = CodexClient(auth=Auth(), transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_text_and_reasoning_separation_with_usage(self):
        result = terminal(output=[
            {"type": "reasoning", "summary": [{"text": "private thought"}]},
            {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "public "}, {"type": "output_text", "text": "answer"}]},
        ])
        client = self.client(lambda _: sse(
            b": heartbeat\n\n" + event("response.reasoning_summary_text.delta", delta="private delta")
            + event("response.output_text.delta", delta="not duplicated")
            + event("response.completed", result)))
        completion = client.chat.completions.create(**request_options())
        self.assertEqual(completion.choices[0].message.content, "public answer")
        self.assertEqual(completion.choices[0].finish_reason, "stop")
        self.assertEqual((completion.usage.prompt_tokens, completion.usage.completion_tokens), (11, 7))
        self.assertEqual(completion.usage.completion_tokens_details.reasoning_tokens, 2)
        self.assertEqual((completion.model, completion.created, completion.system_fingerprint),
                         ("account-model", 123, "fixture-fingerprint"))
        self.assertNotIn("private", completion.model_dump_json())

    def test_headerless_stream_requires_a_valid_terminal_event(self):
        client = self.client(lambda _: httpx.Response(
            200, content=event("response.completed", terminal("headerless answer"))))
        result = client.chat.completions.create(**request_options())
        self.assertEqual(result.choices[0].message.content, "headerless answer")
        incomplete = self.client(lambda _: httpx.Response(
            200, content=event("response.output_text.delta", delta="unfinished")))
        with self.assertRaises(openai.APIError):
            incomplete.chat.completions.create(**request_options())

    def test_completed_stream_items_are_retained_when_terminal_output_is_empty(self):
        message = terminal("streamed answer")["output"][0]
        call = {"type": "function_call", "call_id": "call_stream", "name": "measure", "arguments": '{"x":2}'}
        client = self.client(lambda _: sse(
            event("response.output_text.delta", delta="streamed answer")
            + event("response.output_item.done", output_index=0, item=message)
            + event("response.output_item.done", output_index=1, item=call)
            + event("response.completed", terminal(output=[]))))
        result = client.chat.completions.create(**request_options())
        self.assertEqual(result.choices[0].message.content, "streamed answer")
        self.assertEqual(result.choices[0].message.tool_calls[0].function.arguments, '{"x":2}')
        self.assertEqual(result.choices[0].finish_reason, "tool_calls")

    def test_multimodal_history_and_function_round_trip(self):
        requests = []
        def respond(request):
            requests.append(json.loads(request.content))
            return sse(event("response.done", terminal(output=[{
                "type": "function_call", "call_id": "call_next", "name": "measure", "arguments": '{"x":2}'
            }])))
        client = self.client(respond)
        messages = [
            {"role": "system", "content": "Follow the task."},
            {"role": "developer", "content": [{"type": "text", "text": "Use measurements."}]},
            {"role": "user", "content": [
                {"type": "text", "text": "Compare"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA", "detail": "high"}},
                {"type": "image_url", "image_url": {"url": "https://images.example/figure.png"}},
            ]},
            {"role": "assistant", "content": "Measuring.", "tool_calls": [
                {"type": "function", "id": "call_first", "function": {"name": "measure", "arguments": '{"x":1}'}}]},
            {"role": "tool", "tool_call_id": "call_first", "content": "42"},
        ]
        tools = [{"type": "function", "function": {"name": "measure", "description": "Measure x",
                  "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}}}}]
        result = client.chat.completions.create(**request_options(messages=messages, tools=tools,
            tool_choice={"type": "function", "function": {"name": "measure"}},
            temperature=0.3, max_tokens=100, max_completion_tokens=200, stop=None))
        call = result.choices[0].message.tool_calls[0]
        self.assertEqual((call.id, call.function.name, json.loads(call.function.arguments)),
                         ("call_next", "measure", {"x": 2}))
        self.assertEqual(result.choices[0].finish_reason, "tool_calls")
        body = requests[0]
        self.assertEqual(body["instructions"], "Follow the task.\n\nUse measurements.")
        self.assertEqual(body["input"], [
            {"role": "user", "content": [
                {"type": "input_text", "text": "Compare"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "high"},
                {"type": "input_image", "image_url": "https://images.example/figure.png", "detail": "auto"}]},
            {"type": "message", "role": "assistant", "status": "completed", "content": [
                {"type": "output_text", "text": "Measuring.", "annotations": []}]},
            {"type": "function_call", "call_id": "call_first", "name": "measure", "arguments": '{"x":1}'},
            {"type": "function_call_output", "call_id": "call_first", "output": "42"},
        ])
        self.assertEqual(body["tool_choice"], {"type": "function", "name": "measure"})
        self.assertEqual(body["tools"], [{"type": "function", **tools[0]["function"], "strict": False}])
        self.assertFalse({"temperature", "max_tokens", "max_completion_tokens"} & body.keys())

    def test_n_independent_choices_and_aggregate_billing(self):
        count = 0
        def respond(request):
            nonlocal count
            count += 1
            self.assertNotIn("n", json.loads(request.content))
            return sse(event("response.completed", terminal(f"answer {count}")))
        completion = self.client(respond).chat.completions.create(**request_options(n=3))
        self.assertEqual([(c.index, c.message.content) for c in completion.choices],
                         [(0, "answer 1"), (1, "answer 2"), (2, "answer 3")])
        self.assertEqual((completion.usage.prompt_tokens, completion.usage.completion_tokens,
                          completion.usage.total_tokens, completion.usage.prompt_tokens_details.cached_tokens),
                         (33, 21, 54, 9))

    def test_incomplete_output_exposes_length_not_success(self):
        client = self.client(lambda _: sse(event("response.incomplete", terminal(
            "partial", status="incomplete", incomplete_details={"reason": "max_output_tokens"}))))
        completion = client.chat.completions.create(**request_options())
        self.assertEqual((completion.choices[0].message.content, completion.choices[0].finish_reason),
                         ("partial", "length"))

    def test_failure_malformed_and_premature_streams_never_return_completion(self):
        cases = [
            event("response.failed", {"error": {"message": "fixture-bearer-secret"}}),
            event("error", message="fixture-bearer-secret"),
            event("response.done", terminal(status="failed")),
            event("response.completed", terminal(output=[])),
            event("response.completed", terminal(output=[{"type": "reasoning", "summary": []}])),
            event("response.output_text.delta", delta="partial answer"),
            b"data: {not-json fixture-bearer-secret}\n\n",
            b"data: [DONE]\n\n",
            b"data: {}\n\n",
            b"not-sse\n\n",
            event("response.completed", terminal())[:-1],
        ]
        for data in cases:
            with self.subTest(data=data[:40]):
                client = self.client(lambda _, data=data: sse(data))
                with self.assertRaises(openai.APIError) as caught:
                    client.chat.completions.create(**request_options())
                error = caught.exception
                self.assertNotIn("fixture-bearer-secret", str(error))
                self.assertNotIn("authorization", error.request.headers)
                self.assertEqual(error.request.content, b"")
                self.assertIsNone(error.body)

    def test_http_errors_and_redirects_are_safe_and_never_followed(self):
        for status, exception in [(401, openai.AuthenticationError), (429, openai.RateLimitError),
                                  (503, openai.InternalServerError), (307, openai.APIStatusError)]:
            requests = []
            def respond(request):
                requests.append(request)
                return httpx.Response(status, headers={"location": "https://attacker.example", "x-request-id": "private"},
                                      text="fixture-bearer-secret")
            with self.subTest(status=status):
                client = self.client(respond)
                with self.assertRaises(exception) as caught:
                    client.chat.completions.create(**request_options())
                error = caught.exception
                self.assertEqual(error.status_code, status)
                self.assertEqual(len(requests), 1)
                self.assertEqual(str(requests[0].url), f"{CODEX_BASE_URL}/responses")
                self.assertNotIn("fixture-bearer-secret", str(error))
                self.assertEqual(error.response.content, b"")
                self.assertNotIn("authorization", error.request.headers)
                self.assertIsNone(error.request_id)

    def test_schema_mapping_and_unsupported_options(self):
        schema = {"name": "result", "strict": True, "schema": {"type": "object", "properties": {}}}
        def respond(request):
            self.assertEqual(json.loads(request.content)["text"]["format"], {"type": "json_schema", **schema})
            return sse(event("response.completed", terminal("{}")))
        client = self.client(respond)
        self.assertEqual(client.chat.completions.create(**request_options(response_format={
            "type": "json_schema", "json_schema": schema})).choices[0].message.content, "{}")
        for option in [{"stream": True}, {"top_p": 0.5}, {"stop": ["STOP"]}, {"seed": 42},
                       {"extra_headers": {"authorization": "secret"}}, {"tools": [{"type": "shell"}]}, {"n": True}]:
            with self.subTest(option=option), self.assertRaises(openai.BadRequestError):
                client.chat.completions.create(**request_options(**option))

    def test_model_catalog_is_actual_account_response(self):
        def respond(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["authorization"], "Bearer fixture-bearer-secret")
            self.assertEqual(request.headers["chatgpt-account-id"], "fixture-account")
            self.assertEqual(request.headers["originator"], "ai-scientist")
            return httpx.Response(200, json={"models": [
                {"slug": "account-only-model", "supported_in_api": False, "visibility": "list"},
                {"slug": "other", "supported_in_api": True, "visibility": "hide"},
            ]})
        self.assertEqual(list_models(auth=Auth(), transport=httpx.MockTransport(respond)), ["account-only-model", "other"])
        self.assertEqual(list_models(auth=Auth(), transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"models": []}))), [])
        with self.assertRaises(openai.APIError):
            list_models(auth=Auth(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": []})))


class BlockingStream(httpx.AsyncByteStream):
    def __init__(self):
        self.entered = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.entered.set()
        yield event("response.output_text.delta", delta="partial")
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class CodexAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_function_completion(self):
        client = CodexAsyncClient(auth=Auth(), transport=httpx.MockTransport(lambda _: sse(event(
            "response.done", terminal(output=[{"type": "function_call", "call_id": "call_a", "name": "measure", "arguments": "{}"}])))))
        try:
            result = await client.chat.completions.create(**request_options(n=2))
            self.assertEqual([c.message.tool_calls[0].function.name for c in result.choices], ["measure", "measure"])
            self.assertEqual(result.usage.total_tokens, 36)
        finally:
            await client.close()

    async def test_headerless_stream_returns_completed_answer(self):
        client = CodexAsyncClient(auth=Auth(), transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=
                event("response.output_item.done", output_index=0, item=terminal("async answer")["output"][0])
                + event("response.completed", terminal(output=[])))))
        try:
            result = await client.chat.completions.create(**request_options())
            self.assertEqual(result.choices[0].message.content, "async answer")
        finally:
            await client.close()

    async def test_cancellation_propagates_and_closes_stream(self):
        stream = BlockingStream()
        client = CodexAsyncClient(auth=Auth(), transport=httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=stream)))
        task = asyncio.create_task(client.chat.completions.create(**request_options()))
        try:
            await asyncio.wait_for(stream.entered.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(stream.closed)
        finally:
            task.cancel()
            await client.close()

    async def test_total_timeout_raises_sdk_timeout_and_closes_stream(self):
        stream = BlockingStream()
        client = CodexAsyncClient(auth=Auth(), transport=httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=stream)))
        try:
            with self.assertRaises(openai.APITimeoutError):
                await client.chat.completions.create(**request_options(timeout=0.1))
            self.assertTrue(stream.closed)
        finally:
            await client.close()
