"""No GPU/network tests: fragmented SSE, tool calls, persistence and export."""
import argparse
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from collector_proxy import Settings, create_app, export_dataset


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, fail=False):
        self.chunks = chunks
        self.fail = fail

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
            await asyncio.sleep(0)
        if self.fail:
            raise httpx.ReadError("mock upstream disconnected")


def sse(delta=None, finish=None, index=0):
    return ("data: " + json.dumps({
        "id": "test-completion", "model": "teacher",
        "choices": [{"index": index, "delta": delta or {}, "finish_reason": finish}],
    }, ensure_ascii=False) + "\r\n\r\n").encode()


def payload(tools=False):
    value = {"model": "teacher", "messages": [{"role": "user", "content": "查询订单"}]}
    if tools:
        value["tools"] = [{"type": "function", "function": {
            "name": "lookup", "parameters": {"type": "object", "properties": {
                "order_id": {"type": "string"}}, "required": ["order_id"], "additionalProperties": False},
        }}]
    return value


def answer(content="你好", finish="stop"):
    return {"id": "test", "model": "teacher", "choices": [{
        "index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish,
    }]}


def records(directory):
    return [json.loads(path.read_text(encoding="utf-8")) for path in directory.glob("raw/*/*/record.json")]


def samples(directory):
    return [json.loads(line) for path in directory.glob("datasets/sft-*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()]


def client(tmp_path, handler, **kwargs):
    return TestClient(create_app(Settings(data_dir=tmp_path, **kwargs), httpx.MockTransport(handler)))


def test_plain_json_raw_passthrough_and_no_auth_key_in_records(tmp_path):
    sent = payload()
    sent["extra_body_custom"] = {"untouched": True}
    raw = json.dumps(sent, ensure_ascii=False).encode()
    original = json.dumps(answer(), ensure_ascii=False).encode()

    def upstream(req):
        assert req.content == raw
        assert req.url == "http://127.0.0.1:8000/v1/chat/completions"
        assert req.headers["authorization"] == "Bearer upstream-secret"
        return httpx.Response(200, content=original, headers={"content-type": "application/json"})

    with client(tmp_path, upstream, proxy_key="proxy-secret", upstream_key="upstream-secret") as proxy:
        response = proxy.post("/v1/chat/completions", content=raw, headers={
            "authorization": "Bearer proxy-secret", "x-session-id": "session-1", "x-task-id": "task-1",
        })
    assert response.content == original
    assert response.headers["x-collector-request-id"]
    assert samples(tmp_path)[0]["completion"][0]["content"] == "你好"
    assert records(tmp_path)[0]["task_id"] == "task-1"
    capture = next(tmp_path.glob("raw/*/*"))
    assert (capture / "request.json").read_bytes() == raw
    assert (capture / "response.body").read_bytes() == original
    assert "proxy-secret" not in (capture / "record.json").read_text(encoding="utf-8")


def test_stream_unicode_parallel_tool_calls_usage_and_reasoning(tmp_path):
    wire = (
        sse({"role": "assistant", "reasoning_content": "教师推理"})
        + sse({"content": "查询中", "tool_calls": [
            {"index": 1, "id": "call_b", "type": "function", "function": {"name": "look", "arguments": '{"order_'}},
            {"index": 0, "id": "call_a", "type": "function", "function": {"name": "lookup", "arguments": '{"order_id":"A'}},
        ]})
        + sse({"tool_calls": [
            {"index": 0, "function": {"arguments": '1"}'}},
            {"index": 1, "function": {"name": "up", "arguments": 'id":"B2"}'}},
        ]})
        + sse(finish="tool_calls")
        + b'data: {"choices":[],"usage":{"total_tokens":123}}\n\n'
        + b"data: [DONE]\n\n"
    )
    # One-byte chunks deliberately split UTF-8 code points and CRLF delimiters.
    with client(tmp_path, lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"},
                stream=Chunks([wire[i:i+1] for i in range(len(wire))]))) as proxy:
        response = proxy.post("/v1/chat/completions", json={**payload(True), "stream": True})
    assert response.content == wire
    record = records(tmp_path)[0]
    assert record["state"] == "complete"
    assert record["sse_done_received"]
    assert record["response"]["usage"]["total_tokens"] == 123
    target = samples(tmp_path)[0]["completion"][0]
    assert target["tool_calls"][0]["function"]["arguments"] == {"order_id": "A1"}
    assert target["tool_calls"][1]["function"]["name"] == "lookup"
    assert "reasoning_content" not in target
    assert record["response"]["choices"][0]["message"]["reasoning_content"] == "教师推理"


@pytest.mark.parametrize("arguments", ['{"wrong":"A"}', '{broken', '{"order_id":123}', '[]'])
def test_bad_tool_arguments_are_retained_raw_but_not_exported(tmp_path, arguments):
    response = answer(None, "tool_calls")
    response["choices"][0]["message"]["tool_calls"] = [{
        "id": "call1", "type": "function", "function": {"name": "lookup", "arguments": arguments},
    }]
    with client(tmp_path, lambda req: httpx.Response(200, json=response)) as proxy:
        assert proxy.post("/v1/chat/completions", json=payload(True)).status_code == 200
    assert not samples(tmp_path)
    assert records(tmp_path)[0]["skip_reasons"]


@pytest.mark.parametrize("finish", ["length", "content_filter", None])
def test_truncation_and_filtered_answers_not_exported(tmp_path, finish):
    with client(tmp_path, lambda req: httpx.Response(200, json=answer(finish=finish))) as proxy:
        proxy.post("/v1/chat/completions", json=payload())
    assert not samples(tmp_path)
    assert records(tmp_path)[0]["skip_reasons"] == ["unfinished_or_filtered_choice"]


def test_previous_tool_results_and_argument_normalization(tmp_path):
    request = payload(True)
    request["messages"] += [
        {"role": "assistant", "tool_calls": [{"id": "call1", "type": "function", "function": {
            "name": "lookup", "arguments": '{"order_id":"A"}'}}]},
        {"role": "tool", "tool_call_id": "call1", "content": '{"status":"已发货"}'},
    ]
    with client(tmp_path, lambda req: httpx.Response(200, json=answer("已发货"))) as proxy:
        proxy.post("/v1/chat/completions", json=request)
    row = samples(tmp_path)[0]
    assert row["prompt"][1]["tool_calls"][0]["function"]["arguments"] == {"order_id": "A"}
    assert row["prompt"][2]["role"] == "tool"
    assert row["completion"][0]["content"] == "已发货"


def test_http_and_sse_errors_do_not_become_samples(tmp_path):
    def upstream(req):
        if json.loads(req.content).get("stream"):
            wire = sse({"content": "partial"}) + b'data: {"error":{"message":"failed"}}\n\n'
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks([wire]))
        return httpx.Response(429, json={"error": {"message": "busy"}}, headers={"retry-after": "2"})
    with client(tmp_path, upstream) as proxy:
        response = proxy.post("/v1/chat/completions", json=payload())
        assert response.status_code == 429
        assert response.headers["retry-after"] == "2"
        proxy.post("/v1/chat/completions", json={**payload(), "stream": True})
    assert len(records(tmp_path)) == 2
    assert not samples(tmp_path)


def test_upstream_connection_failure_saved(tmp_path):
    def upstream(req):
        raise httpx.ConnectError("unavailable")
    with client(tmp_path, upstream) as proxy:
        assert proxy.post("/v1/chat/completions", json=payload()).status_code == 502
    assert records(tmp_path)[0]["state"] == "incomplete"
    assert not samples(tmp_path)


def test_interrupted_stream_saved_but_not_exported(tmp_path):
    app = create_app(Settings(data_dir=tmp_path), httpx.MockTransport(lambda req: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Chunks([sse({"content": "partial"})], fail=True),
    )))
    with TestClient(app, raise_server_exceptions=False) as proxy:
        proxy.post("/v1/chat/completions", json={**payload(), "stream": True})
    assert records(tmp_path)[0]["state"] == "incomplete"
    assert not samples(tmp_path)


def test_auth_models_and_invalid_request(tmp_path):
    with client(tmp_path, lambda req: httpx.Response(200, json={"data": []}), proxy_key="key") as proxy:
        assert proxy.get("/healthz").status_code == 200
        assert proxy.get("/v1/models").status_code == 401
        headers = {"authorization": "Bearer key"}
        assert proxy.get("/v1/models", headers=headers).json() == {"data": []}
        assert proxy.post("/v1/chat/completions", json={}, headers=headers).status_code == 400
    assert not records(tmp_path)


def test_offline_export_deduplicates_and_recovers_reasoning(tmp_path):
    response = answer()
    response["choices"][0]["message"]["reasoning_content"] = "teacher reasoning"
    with client(tmp_path, lambda req: httpx.Response(200, json=response)) as proxy:
        for _ in range(2):
            proxy.post("/v1/chat/completions", json=payload())
    output = tmp_path / "exports" / "train.jsonl"
    args = argparse.Namespace(data_dir=tmp_path, output=output, overwrite=False,
        include_reasoning=True, format="messages", keep_duplicates=False)
    export_dataset(args)
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["messages"][-1]["reasoning_content"] == "teacher reasoning"
    assert json.loads(output.with_suffix(".jsonl.stats.json").read_text())["counts"]["duplicates"] == 1


def test_multiple_choices_are_separate_samples(tmp_path):
    response = answer()
    response["choices"].append({"index": 1, "message": {"role": "assistant", "content": "第二个"}, "finish_reason": "stop"})
    with client(tmp_path, lambda req: httpx.Response(200, json=response)) as proxy:
        proxy.post("/v1/chat/completions", json={**payload(), "n": 2})
    assert len(samples(tmp_path)) == 2


def test_client_disconnect_finalizes_capture(tmp_path):
    async def scenario():
        app = create_app(Settings(data_dir=tmp_path), httpx.MockTransport(lambda req: httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=Chunks([sse({"content": "first"}), sse(finish="stop"), b"data: [DONE]\n\n"]),
        )))
        async def receive():
            return {"type": "http.request", "body": json.dumps({**payload(), "stream": True}).encode(), "more_body": False}
        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                raise OSError("mock client disconnected")
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "method": "POST", "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
                 "query_string": b"", "headers": [(b"content-type", b"application/json")],
                 "scheme": "http", "server": ("localhost", 9000), "client": ("localhost", 12345), "http_version": "1.1"}
        async with app.router.lifespan_context(app):
            with pytest.raises(Exception):
                await app(scope, receive, send)
    asyncio.run(scenario())
    assert records(tmp_path)[0]["state"] == "incomplete"
    assert not samples(tmp_path)


def test_concurrent_captures_produce_whole_jsonl_rows(tmp_path):
    async def scenario():
        async def handler(req):
            await asyncio.sleep(0.005)
            text = json.loads(req.content)["messages"][0]["content"]
            return httpx.Response(200, json=answer(text))
        app = create_app(Settings(data_dir=tmp_path), httpx.MockTransport(handler))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as proxy:
                requests = []
                for index in range(20):
                    item = payload()
                    item["messages"][0]["content"] = str(index)
                    requests.append(proxy.post("/v1/chat/completions", json=item))
                responses = await asyncio.gather(*requests)
                assert all(response.status_code == 200 for response in responses)
    asyncio.run(scenario())
    assert len(records(tmp_path)) == 20
    rows = samples(tmp_path)
    assert len(rows) == 20
    assert {row["completion"][0]["content"] for row in rows} == {str(i) for i in range(20)}


def test_first_stream_chunk_is_forwarded_before_upstream_finishes(tmp_path):
    async def scenario():
        first_forwarded = asyncio.Event()

        class GatedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield sse({"content": "first"})
                # Upstream cannot finish until the client has received its first chunk.
                await asyncio.wait_for(first_forwarded.wait(), timeout=3)
                yield sse(finish="stop") + b"data: [DONE]\n\n"

        app = create_app(Settings(data_dir=tmp_path), httpx.MockTransport(lambda req: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=GatedStream(),
        )))
        async def receive():
            return {"type": "http.request", "body": json.dumps({**payload(), "stream": True}).encode(), "more_body": False}
        received = []
        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                received.append(message["body"])
                first_forwarded.set()
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "method": "POST", "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
                 "query_string": b"", "headers": [(b"content-type", b"application/json")],
                 "scheme": "http", "server": ("localhost", 9000), "client": ("localhost", 12345), "http_version": "1.1"}
        async with app.router.lifespan_context(app):
            await app(scope, receive, send)
        assert len(received) == 2
    asyncio.run(scenario())
    assert records(tmp_path)[0]["state"] == "complete"
    assert len(samples(tmp_path)) == 1
