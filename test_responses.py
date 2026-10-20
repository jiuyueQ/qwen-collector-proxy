"""Responses protocol, durable lineage, branches, checkpoint races and exports."""
import argparse
import asyncio
import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from collector_proxy import Settings, create_app, export_dataset
from responses_support import ResponseHistory
from test_proxy import Chunks, records, samples


def message(text, role="assistant"):
    return {"type": "message", "role": role, "status": "completed",
            "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}]}


def call(call_id="call_A", name="lookup", arguments='{"order_id":"A123"}'):
    return {"type": "function_call", "id": "fc_" + call_id, "call_id": call_id,
            "name": name, "arguments": arguments, "status": "completed"}


def response(response_id, output=None, status="completed"):
    return {"object": "response", "id": response_id, "status": status, "model": "teacher",
            "output": [message("完成")] if output is None else output, "error": None}


TOOLS = [{"type": "function", "name": "lookup", "description": "查询订单", "parameters": {
    "type": "object", "properties": {"order_id": {"type": "string"}},
    "required": ["order_id"], "additionalProperties": False,
}}]


def event(kind, **kwargs):
    return ("event: " + kind + "\r\ndata: " + json.dumps({"type": kind, **kwargs}, ensure_ascii=False) + "\r\n\r\n").encode()


def native_rows(directory):
    return [json.loads(line) for path in directory.glob("datasets/responses-native-*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()]


def proxy(directory, handler, **kwargs):
    return TestClient(create_app(Settings(data_dir=directory, **kwargs), httpx.MockTransport(handler)))


def test_nonstream_responses_preserves_bytes_and_function_parameters(tmp_path):
    request = {"model": "teacher", "input": "查询 A123", "instructions": "使用工具", "tools": TOOLS}
    wire = json.dumps(response("r1", [call()]), ensure_ascii=False).encode()
    def handler(req):
        assert req.url.path == "/v1/responses"
        assert json.loads(req.content) == request
        return httpx.Response(200, content=wire, headers={"content-type": "application/json"})
    with proxy(tmp_path, handler) as client:
        received = client.post("/v1/responses", json=request)
    assert received.content == wire
    row = samples(tmp_path)[0]
    assert row["prompt"][0] == {"role": "system", "content": "使用工具"}
    assert row["completion"][0]["tool_calls"][0]["id"] == "call_A"
    assert row["completion"][0]["tool_calls"][0]["function"]["arguments"] == {"order_id": "A123"}
    assert row["tools"][0]["function"]["name"] == "lookup"
    assert native_rows(tmp_path)[0]["metadata"]["history_complete"]


def test_fragmented_responses_sse_parallel_calls_and_final_authority(tmp_path):
    calls = [call("a"), call("b", arguments='{"order_id":"B2"}')]
    wire = event("response.created", response=response("r_stream", [], "in_progress"))
    for index, item in enumerate(calls):
        added = {**item, "arguments": "", "status": "in_progress"}
        wire += event("response.output_item.added", output_index=index, item=added)
    for index, item in enumerate(calls):
        wire += event("response.function_call_arguments.delta", output_index=index, item_id=item["id"], delta=item["arguments"][:8])
    for index, item in enumerate(calls):
        wire += event("response.function_call_arguments.delta", output_index=index, item_id=item["id"], delta=item["arguments"][8:])
        wire += event("response.function_call_arguments.done", output_index=index, item_id=item["id"], arguments=item["arguments"])
        wire += event("response.output_item.done", output_index=index, item=item)
    final = response("r_stream", calls)
    final["usage"] = {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}
    wire += event("response.completed", response=final)
    with proxy(tmp_path, lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"},
               stream=Chunks([wire[i:i+1] for i in range(len(wire))]))) as client:
        result = client.post("/v1/responses", json={"model": "teacher", "input": "查询两个订单", "tools": TOOLS, "stream": True})
    assert result.content == wire
    target = samples(tmp_path)[0]["completion"][0]
    assert [item["id"] for item in target["tool_calls"]] == ["a", "b"]
    assert records(tmp_path)[0]["response"] == final
    assert records(tmp_path)[0]["protocol_completed"]


def test_stream_text_and_reasoning_summary_fallback_without_terminal_output(tmp_path):
    wire = event("response.created", response=response("r_reason", [], "in_progress"))
    wire += event("response.output_item.added", output_index=0, item={"type": "reasoning", "id": "reason1", "summary": []})
    wire += event("response.reasoning_summary_part.added", output_index=0, summary_index=0, part={"type": "summary_text", "text": ""})
    wire += event("response.reasoning_summary_text.delta", output_index=0, summary_index=0, delta="简要依据")
    wire += event("response.output_item.done", output_index=0, item={"type": "reasoning", "id": "reason1",
        "summary": [{"type": "summary_text", "text": "简要依据"}]})
    wire += event("response.output_item.added", output_index=1, item={"type": "message", "role": "assistant", "content": [], "status": "in_progress"})
    wire += event("response.content_part.added", output_index=1, content_index=0, part={"type": "output_text", "text": ""})
    for text in ("你", "好"):
        wire += event("response.output_text.delta", output_index=1, content_index=0, delta=text)
    wire += event("response.output_text.done", output_index=1, content_index=0, text="你好")
    wire += event("response.output_item.done", output_index=1, item=message("你好"))
    terminal = response("r_reason")
    del terminal["output"]
    wire += event("response.completed", response=terminal)
    with proxy(tmp_path, lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks([wire])),
            include_reasoning=True) as client:
        client.post("/v1/responses", json={"model": "teacher", "input": "你好", "stream": True})
    assert samples(tmp_path)[0]["completion"][0]["content"] == "你好"
    assert samples(tmp_path)[0]["completion"][0]["reasoning_content"] == "简要依据"


def test_three_turn_tool_history_restart_and_branch_isolation(tmp_path):
    def handler(req):
        body = json.loads(req.content)
        previous = body.get("previous_response_id")
        if not previous:
            return httpx.Response(200, json=response("r1", [call()]))
        if previous == "r1":
            if body.get("metadata", {}).get("branch"):
                return httpx.Response(200, json=response("branch", [message("分支答案")]))
            return httpx.Response(200, json=response("r2", [call("call_B", arguments='{"order_id":"B"}')]))
        return httpx.Response(200, json=response("r3", [message("订单已发货")]))
    with proxy(tmp_path, handler) as client:
        client.post("/v1/responses", json={"input": "查订单", "instructions": "旧指令", "tools": TOOLS})
    # A fresh app process has only persistent files/index, not the first app's memory.
    with proxy(tmp_path, handler) as client:
        result_a = {"type": "function_call_output", "call_id": "call_A", "output": "A 已发货"}
        client.post("/v1/responses", json={"previous_response_id": "r1", "input": [result_a], "tools": TOOLS})
        client.post("/v1/responses", json={"previous_response_id": "r1", "input": [result_a], "metadata": {"branch": True}})
        client.post("/v1/responses", json={"previous_response_id": "r2", "input": [
            {"type": "function_call_output", "call_id": "call_B", "output": "B 已发货"}], "instructions": "新指令"})
    final = next(row for row in samples(tmp_path) if row["metadata"]["response_id"] == "r3")
    assert [m["role"] for m in final["prompt"]] == ["system", "user", "assistant", "tool", "assistant", "tool"]
    assert final["prompt"][0]["content"] == "新指令"
    assert "旧指令" not in json.dumps(final, ensure_ascii=False)
    assert "分支答案" not in json.dumps(final, ensure_ascii=False)
    assert final["metadata"]["ancestor_response_ids"] == ["r1", "r2"]
    middle = next(row for row in samples(tmp_path) if row["metadata"]["response_id"] == "r2")
    assert all(m["role"] != "system" for m in middle["prompt"])


def test_missing_parent_retained_and_reexport_resolves_late_parent(tmp_path):
    with proxy(tmp_path, lambda req: httpx.Response(200, json=response("child"))) as client:
        client.post("/v1/responses", json={"input": "追问", "previous_response_id": "parent"})
    assert not samples(tmp_path)
    assert not native_rows(tmp_path)
    assert records(tmp_path)[0]["skip_reasons"][0].startswith("history_unresolved:")
    with proxy(tmp_path, lambda req: httpx.Response(200, json=response("parent", [message("原答案")]))) as client:
        client.post("/v1/responses", json={"input": "原问题"})
    output = tmp_path / "exports" / "complete.jsonl"
    export_dataset(argparse.Namespace(data_dir=tmp_path, output=output, overwrite=False,
        include_reasoning=False, format="prompt-completion", keep_duplicates=False))
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    child = next(row for row in rows if row["metadata"]["response_id"] == "child")
    assert [m["content"] for m in child["prompt"]] == ["原问题", "原答案", "追问"]


@pytest.mark.parametrize("status", ["in_progress", "queued", "failed", "incomplete", "cancelled"])
def test_non_completed_responses_not_exported(tmp_path, status):
    with proxy(tmp_path, lambda req: httpx.Response(200, json=response("not_done", status=status))) as client:
        assert client.post("/v1/responses", json={"input": "问题"}).status_code == 200
    assert not samples(tmp_path)
    assert not native_rows(tmp_path)


def test_background_retrieve_and_lifecycle_forwarding(tmp_path):
    def handler(req):
        if req.url.path.endswith("/cancel"):
            return httpx.Response(200, json=response("r_bg", status="cancelled"))
        if req.method == "DELETE":
            return httpx.Response(200, json={"id": "r_bg", "object": "response.deleted", "deleted": True})
        if req.method == "GET":
            return httpx.Response(200, json=response("r_bg", [message("后台完成")]))
        body = json.loads(req.content)
        if body.get("previous_response_id"):
            return httpx.Response(200, json=response("r_child"))
        return httpx.Response(200, json=response("r_bg", [], "queued"))
    with proxy(tmp_path, handler) as client:
        client.post("/v1/responses", json={"input": "后台问题", "background": True, "store": True})
        assert not samples(tmp_path)
        assert client.get("/v1/responses/r_bg").status_code == 200
        client.post("/v1/responses", json={"input": "追问", "previous_response_id": "r_bg"})
        assert client.post("/v1/responses/r_bg/cancel").status_code == 200
        assert client.delete("/v1/responses/r_bg").status_code == 200
    child = next(row for row in samples(tmp_path) if row["metadata"]["response_id"] == "r_child")
    assert [m["content"] for m in child["prompt"]] == ["后台问题", "后台完成", "追问"]
    assert len(records(tmp_path)) == 5


def test_unsupported_native_items_are_not_silently_discarded(tmp_path):
    output = [{"type": "web_search_call", "id": "search1", "status": "completed"}, message("搜索结果")]
    with proxy(tmp_path, lambda req: httpx.Response(200, json=response("native", output))) as client:
        client.post("/v1/responses", json={"input": "搜索问题", "tools": [{"type": "web_search"}]})
    assert not samples(tmp_path)
    assert native_rows(tmp_path)[0]["output"] == output


def test_unmatched_tool_output_not_converted_to_sft(tmp_path):
    with proxy(tmp_path, lambda req: httpx.Response(200, json=response("orphan"))) as client:
        client.post("/v1/responses", json={"input": [{"type": "function_call_output", "call_id": "missing", "output": "结果"}]})
    assert not samples(tmp_path)
    assert len(native_rows(tmp_path)) == 1


def test_scope_isolation_and_index_rebuild(tmp_path):
    with proxy(tmp_path, lambda req: httpx.Response(200, json=response("root")), upstream_key="key1") as client:
        client.post("/v1/responses", json={"input": "租户1"})
    history = ResponseHistory(tmp_path)
    with history.connect() as db:
        db.execute("DELETE FROM responses")
    with proxy(tmp_path, lambda req: httpx.Response(200, json=response("child")), upstream_key="key1") as client:
        client.post("/v1/responses", json={"input": "追问", "previous_response_id": "root"})
    assert len(samples(tmp_path)) == 2
    with proxy(tmp_path, lambda req: httpx.Response(200, json=response("other")), upstream_key="key2") as client:
        client.post("/v1/responses", json={"input": "租户2", "previous_response_id": "root"})
    assert len(samples(tmp_path)) == 2


def test_stream_without_terminal_event_and_partial_network_failure(tmp_path):
    wire = event("response.created", response=response("partial", [], "in_progress"))
    wire += event("response.output_item.done", output_index=0, item=message("部分回答"))
    with proxy(tmp_path, lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks([wire]))) as client:
        client.post("/v1/responses", json={"input": "问题", "stream": True})
    assert not samples(tmp_path)
    assert "missing_responses_terminal_event" in records(tmp_path)[0]["parse_errors"]


def test_parent_checkpoint_visible_before_parent_stream_finishes(tmp_path):
    async def scenario():
        parent = response("parent", [message("父回复")])
        wire = event("response.created", response=response("parent", [], "in_progress"))
        terminal = event("response.completed", response=parent)
        def handler(req):
            body = json.loads(req.content)
            if body.get("previous_response_id"):
                return httpx.Response(200, json=response("child", [message("子回复")]))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks([wire, terminal]))
        app = create_app(Settings(data_dir=tmp_path), httpx.MockTransport(handler))
        nested_sent = False
        async def receive():
            return {"type": "http.request", "body": json.dumps({"input": "父问题", "stream": True}).encode(), "more_body": False}
        async def send(packet):
            nonlocal nested_sent
            if packet["type"] == "http.response.body" and b"response.completed" in packet.get("body", b"") and not nested_sent:
                nested_sent = True
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as nested:
                    result = await nested.post("/v1/responses", json={"input": "追问", "previous_response_id": "parent"})
                    assert result.status_code == 200
                    assert any(row["metadata"]["response_id"] == "child" for row in samples(tmp_path))
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "method": "POST",
                 "path": "/v1/responses", "raw_path": b"/v1/responses", "query_string": b"",
                 "headers": [(b"content-type", b"application/json")], "scheme": "http", "server": ("localhost", 9000),
                 "client": ("localhost", 1), "http_version": "1.1"}
        async with app.router.lifespan_context(app):
            await app(scope, receive, send)
    asyncio.run(scenario())
    child = next(row for row in samples(tmp_path) if row["metadata"]["response_id"] == "child")
    assert [m["content"] for m in child["prompt"]] == ["父问题", "父回复", "追问"]


def test_native_offline_export_is_complete_and_deduplicates_polling(tmp_path):
    def handler(req):
        return httpx.Response(200, json=response("root"))
    with proxy(tmp_path, handler) as client:
        client.post("/v1/responses", json={"input": "问题"})
        client.get("/v1/responses/root")
    output = tmp_path / "exports" / "native.jsonl"
    export_dataset(argparse.Namespace(data_dir=tmp_path, output=output, overwrite=False,
        include_reasoning=False, format="responses-native", keep_duplicates=False))
    assert len(output.read_text(encoding="utf-8").splitlines()) == 1


def test_cycles_and_stateful_conversation_not_guessed(tmp_path):
    def handler(req):
        request = json.loads(req.content)
        return httpx.Response(200, json=response(request["metadata"]["id"]))
    with proxy(tmp_path, handler) as client:
        client.post("/v1/responses", json={"input": "一", "previous_response_id": "r2", "metadata": {"id": "r1"}})
        client.post("/v1/responses", json={"input": "二", "previous_response_id": "r1", "metadata": {"id": "r2"}})
        client.post("/v1/responses", json={"input": "三", "conversation": "conv_external", "metadata": {"id": "r3"}})
    assert not samples(tmp_path)
    assert not native_rows(tmp_path)
    reasons = [reason for record in records(tmp_path) for reason in record["skip_reasons"]]
    assert any("history_cycle" in reason for reason in reasons)
    assert any("server_conversation_not_resolved" in reason for reason in reasons)


def test_response_auth_and_bad_arguments_skip_only_sft(tmp_path):
    with proxy(tmp_path, lambda req: httpx.Response(200, json=response("bad", [call(arguments='{"wrong":1}')])),
               proxy_key="proxy-key") as client:
        assert client.post("/v1/responses", json={"input": "问题"}).status_code == 401
        result = client.post("/v1/responses", json={"input": "问题", "tools": TOOLS}, headers={"authorization": "Bearer proxy-key"})
        assert result.status_code == 200
    assert len(records(tmp_path)) == 1
    assert not samples(tmp_path)
    assert len(native_rows(tmp_path)) == 1
