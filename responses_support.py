"""Durable Responses lineage and lossless/native -> conversational export."""
from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


class HistoryError(ValueError):
    pass


class ResponseHistory:
    """Index references raw files, not an in-memory session guess. Safe across workers."""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir).resolve()
        directory = self.root / "state"
        directory.mkdir(parents=True, exist_ok=True)
        self.database = directory / "responses.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS responses (
                scope TEXT NOT NULL, response_id TEXT NOT NULL,
                origin TEXT, completed TEXT, PRIMARY KEY(scope, response_id)
            )""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def publish(self, directory: Path, record: dict[str, Any]) -> None:
        response = record.get("response") or {}
        if not isinstance(response, dict) or not isinstance(response.get("id"), str):
            return
        if not 200 <= (record.get("upstream_status") or 0) < 300 or record.get("parse_errors"):
            return
        if not record.get("operation", "").startswith("responses."):
            return
        scope = record.get("history_scope", "")
        relative = directory.resolve().relative_to(self.root).as_posix()
        origin = relative if record["operation"] == "responses.create" else None
        completed = relative if record.get("protocol_completed") and response.get("status") == "completed" else None
        with self.connect() as db:
            existing = db.execute("SELECT origin FROM responses WHERE scope=? AND response_id=?",
                                  (scope, response["id"])).fetchone()
            if origin and existing and existing[0] and existing[0] != origin:
                # A response ID must identify one creation. Never mix conflicting origins.
                raise HistoryError("response_id_collision:" + response["id"])
            db.execute("""INSERT INTO responses(scope,response_id,origin,completed) VALUES(?,?,?,?)
                ON CONFLICT(scope,response_id) DO UPDATE SET
                    origin=COALESCE(responses.origin,excluded.origin),
                    completed=COALESCE(excluded.completed,responses.completed)""",
                       (scope, response["id"], origin, completed))

    def node(self, scope: str, response_id: str) -> tuple[dict, dict]:
        with self.connect() as db:
            entry = db.execute("SELECT origin,completed FROM responses WHERE scope=? AND response_id=?",
                               (scope, response_id)).fetchone()
        if not entry or not entry[0] or not entry[1]:
            raise HistoryError("missing_previous_response_id:" + response_id)
        origin_dir = self.root / entry[0]
        completed_dir = self.root / entry[1]
        try:
            request = json.loads((origin_dir / "request.json").read_bytes())
            record = json.loads((completed_dir / "record.json").read_bytes())
            if (not record.get("protocol_completed") or record.get("parse_errors")
                    or record["response"].get("status") != "completed"):
                raise HistoryError("previous_response_not_completed:" + response_id)
            return request, record
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HistoryError("unreadable_previous_response:" + response_id) from exc

    def origin(self, scope: str, response_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT origin FROM responses WHERE scope=? AND response_id=?",
                             (scope, response_id)).fetchone()
        if not row or not row[0]:
            raise HistoryError("missing_creation_request:" + response_id)
        try:
            return json.loads((self.root / row[0] / "request.json").read_bytes())
        except (OSError, ValueError) as exc:
            raise HistoryError("unreadable_creation_request:" + response_id) from exc

    def rebuild(self) -> None:
        # Two passes during offline export; file order and UUID ordering don't matter.
        for path in sorted((self.root / "raw").glob("*/*/record.json")):
            try:
                self.publish(path.parent, json.loads(path.read_bytes()))
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def resolve(self, request: dict, scope: str, response_id: str | None,
                max_turns: int = 1024) -> tuple[list[dict], list[str]]:
        nodes = []
        seen = {response_id} if response_id else set()
        previous = request.get("previous_response_id")
        while previous:
            if not isinstance(previous, str):
                raise HistoryError("invalid_previous_response_id")
            if previous in seen:
                raise HistoryError("history_cycle:" + previous)
            if len(nodes) >= max_turns:
                raise HistoryError("history_depth_limit")
            seen.add(previous)
            ancestor_request, ancestor_record = self.node(scope, previous)
            if ancestor_request.get("conversation"):
                raise HistoryError("server_conversation_not_resolved")
            nodes.append((previous, ancestor_request, ancestor_record))
            previous = ancestor_request.get("previous_response_id")
        if request.get("conversation"):
            raise HistoryError("server_conversation_not_resolved")
        if request.get("previous_input_messages") or any(node[1].get("previous_input_messages") for node in nodes):
            raise HistoryError("unsupported_previous_input_messages")
        items: list[dict] = []
        chain: list[str] = []
        for previous, ancestor_request, ancestor_record in reversed(nodes):
            items.extend(input_items(ancestor_request.get("input", [])))
            items.extend(copy.deepcopy(ancestor_record["response"].get("output") or []))
            chain.append(previous)
        items.extend(input_items(request.get("input", [])))
        # Top-level instructions are intentionally NOT inherited across previous_response_id.
        return items, chain


def input_items(value: Any) -> list[dict]:
    if isinstance(value, str):
        return [{"type": "message", "role": "user", "content": value}]
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise HistoryError("invalid_responses_input")
    return copy.deepcopy(value)


def text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError("unsupported_responses_content")
    chunks = []
    for part in content:
        if part.get("type") in {"input_text", "output_text", "text"}:
            chunks.append(part["text"])
        else:
            # Do not silently drop images, refusals, files, or other content.
            raise ValueError("unsupported_content_part:" + str(part.get("type")))
    return "".join(chunks)


def items_to_messages(items: list[dict], include_reasoning: bool) -> list[dict]:
    messages: list[dict] = []
    pending_reasoning = ""
    known_calls: set[str] = set()
    completed_calls: set[str] = set()

    def assistant() -> dict:
        nonlocal pending_reasoning
        if not messages or messages[-1].get("role") != "assistant":
            messages.append({"role": "assistant", "content": ""})
        message = messages[-1]
        if pending_reasoning:
            message["reasoning_content"] = message.get("reasoning_content", "") + pending_reasoning
            pending_reasoning = ""
        return message

    for item in items:
        kind = item.get("type", "message" if item.get("role") else "")
        if item.get("status") not in (None, "completed"):
            raise ValueError("incomplete_output_item")
        if kind == "reasoning":
            if include_reasoning:
                parts = item.get("summary", []) + item.get("content", [])
                visible = [part["text"] for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]
                pending_reasoning += "\n".join(visible)
        elif kind == "message":
            role = item.get("role")
            content = text_content(item.get("content", ""))
            if role == "assistant":
                message = assistant()
                message["content"] += ("\n" if message["content"] and content else "") + content
            elif role in {"user", "system", "developer"}:
                if pending_reasoning:
                    raise ValueError("unattached_reasoning")
                messages.append({"role": role, "content": content})
            else:
                raise ValueError("unsupported_response_message_role")
        elif kind == "function_call":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id or call_id in known_calls:
                raise ValueError("missing_or_duplicate_function_call_id")
            known_calls.add(call_id)
            assistant().setdefault("tool_calls", []).append({
                "id": call_id, "type": "function", "function": {
                    "name": item["name"], "arguments": item["arguments"],
                },
            })
        elif kind == "function_call_output":
            call_id = item.get("call_id")
            if call_id not in known_calls or call_id in completed_calls:
                raise ValueError("unmatched_function_call_output")
            if pending_reasoning:
                raise ValueError("unattached_reasoning")
            completed_calls.add(call_id)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": text_content(item["output"])})
        else:
            raise ValueError("unsupported_responses_item:" + str(kind))
    if pending_reasoning:
        assistant()
    return messages


def build_response_exports(request: dict, record: dict, history: ResponseHistory,
                           make_chat_samples: Callable, *, include_reasoning: bool = False,
                           output_format: str = "prompt-completion"
                           ) -> tuple[list[dict], list[dict], list[str]]:
    """Return (SFT samples, complete native samples, skip reasons). Never guess history."""
    if record.get("state") != "complete":
        return [], [], ["transport_incomplete"]
    if not 200 <= (record.get("upstream_status") or 0) < 300:
        return [], [], ["upstream_http_error"]
    if record.get("parse_errors"):
        return [], [], ["response_parse_error"]
    if record.get("operation") in {"responses.cancel", "responses.delete"}:
        return [], [], ["lifecycle_operation"]
    response = record.get("response") or {}
    if not isinstance(response, dict) or response.get("status") != "completed" or response.get("error"):
        return [], [], ["responses_not_completed"]
    if not record.get("protocol_completed"):
        return [], [], ["missing_responses_terminal_event"]
    if not isinstance(response.get("id"), str) or not response["id"]:
        return [], [], ["missing_response_id"]
    if not isinstance(response.get("output"), list):
        return [], [], ["missing_responses_output"]
    scope = record.get("history_scope", "")
    try:
        if record.get("operation") == "responses.retrieve":
            request = history.origin(scope, response["id"])
        items, chain = history.resolve(request, scope, response.get("id"))
        # References require a separate server item store; don't label opaque references as full history.
        if any(item.get("type") == "item_reference" for item in items):
            raise HistoryError("unresolved_item_reference")
    except (ValueError, KeyError, TypeError) as exc:
        return [], [], ["history_unresolved:" + str(exc)]
    meta = {
        "request_id": record["request_id"], "api": "responses", "response_id": response.get("id"),
        "previous_response_id": request.get("previous_response_id"), "ancestor_response_ids": chain,
        "root_response_id": chain[0] if chain else response.get("id"), "history_complete": True,
        "history_kind": "logical_response_chain", "captured_at": record.get("started_at"),
        "session_id": record.get("session_id"), "task_id": record.get("task_id"),
        "business_verified": False,
    }
    native = {"input": items, "output": copy.deepcopy(response["output"]), "metadata": meta,
              "model": response.get("model") or request.get("model")}
    if response.get("usage") is not None:
        native["usage"] = copy.deepcopy(response["usage"])
    for key in ("instructions", "tools", "tool_choice", "parallel_tool_calls", "truncation"):
        if key in request:
            native[key] = copy.deepcopy(request[key])
    try:
        instructions = request.get("instructions")
        initial = []
        if isinstance(instructions, str) and instructions:
            initial = [{"role": "system", "content": instructions}]
        elif instructions:
            initial = items_to_messages(input_items(instructions), include_reasoning)
        prompt = initial + items_to_messages(items, include_reasoning)
        # Convert together to validate tool outputs against calls from previous responses.
        combined = items_to_messages(items + response["output"], include_reasoning)
        prompt_without_instructions = items_to_messages(items, include_reasoning)
        if combined[:len(prompt_without_instructions)] != prompt_without_instructions:
            raise ValueError("response_output_merges_with_input_assistant")
        completion = combined[len(prompt_without_instructions):]
        if not completion or any(message.get("role") != "assistant" for message in completion):
            raise ValueError("unsupported_responses_completion_sequence")
        tools = []
        for tool in request.get("tools") or []:
            if tool.get("type") != "function":
                raise ValueError("unsupported_training_tool:" + str(tool.get("type")))
            tools.append({"type": "function", "function": {
                key: copy.deepcopy(value) for key, value in tool.items() if key != "type"
            }})
        converted_request = {"messages": prompt, "model": native["model"], "tools": tools,
                             "tool_choice": request.get("tool_choice"),
                             "parallel_tool_calls": request.get("parallel_tool_calls")}
        converted_record = {**record, "response": {"model": native["model"], "choices": [{
            "index": 0, "message": completion[0],
            "finish_reason": "tool_calls" if completion[0].get("tool_calls") else "stop",
        }]}}
        if len(completion) != 1:
            raise ValueError("multiple_assistant_targets")
        samples, reasons = make_chat_samples(converted_request, converted_record,
            include_reasoning=include_reasoning, output_format=output_format)
        for sample in samples:
            sample["metadata"].update(meta)
        return samples, [native], reasons
    except Exception as exc:
        return [], [native], ["responses_sft_conversion:" + type(exc).__name__ + ":" + str(exc)[:200]]
