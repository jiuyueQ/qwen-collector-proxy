#!/usr/bin/env python3
"""OpenAI Chat Completions + Responses recording proxy. Python >=3.10.

serve: transparent forwarding + raw capture + automatic candidate SFT export.
export: rebuild a dataset from completed captures, with optional deduplication.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import hmac
import json
import logging
import os
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from filelock import FileLock
from jsonschema.validators import validator_for
from responses_support import ResponseHistory, build_response_exports

LOG = logging.getLogger("collector")


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def atomic_json(path: Path, value: Any) -> None:
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(dump(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@dataclass
class Settings:
    upstream: str = "http://127.0.0.1:8000/v1"
    data_dir: Path = Path("./data")
    proxy_key: str = ""
    upstream_key: str = ""
    read_timeout: float = 600.0
    max_request_bytes: int = 32 * 1024 * 1024
    include_reasoning: bool = False

    def __post_init__(self) -> None:
        self.upstream = self.upstream.rstrip("/")
        self.data_dir = Path(self.data_dir).resolve()
        url = httpx.URL(self.upstream)
        if url.scheme not in ("http", "https") or not url.host:
            raise ValueError("upstream 必须是 http(s) URL，例如 http://127.0.0.1:8000/v1")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("upstream URL 不接受凭据、query 或 fragment；密钥使用环境变量")


class SSECollector:
    """Incremental UTF-8 SSE parser; stream bytes themselves are never changed."""

    def __init__(self) -> None:
        import codecs

        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.buffer = ""
        self.data: list[str] = []
        self.data_size = 0
        self.choices: dict[int, dict[str, Any]] = {}
        self.info: dict[str, Any] = {}
        self.errors: list[str] = []
        self.done = False
        self.disabled = False

    def error(self, message: str) -> None:
        if len(self.errors) < 10:
            self.errors.append(message)

    def feed(self, chunk: bytes, final: bool = False) -> None:
        if self.disabled:
            return
        try:
            self.buffer += self.decoder.decode(chunk, final=final)
            while "\n" in self.buffer:
                line, self.buffer = self.buffer.split("\n", 1)
                self.line(line.rstrip("\r"))
            if len(self.buffer) > 8 * 1024 * 1024:
                raise ValueError("SSE line exceeds 8 MiB")
            if final:
                if self.buffer:
                    self.line(self.buffer.rstrip("\r"))
                    self.buffer = ""
                self.dispatch()
        except (UnicodeError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self.error(type(exc).__name__ + ": " + str(exc)[:200])
            self.disabled = True
            self.buffer = ""
            self.data.clear()

    def line(self, line: str) -> None:
        if not line:
            self.dispatch()
        elif line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            self.data.append(value)
            self.data_size += len(value)
            if self.data_size > 8 * 1024 * 1024:
                raise ValueError("SSE event exceeds 8 MiB")

    def dispatch(self) -> None:
        if not self.data:
            return
        text = "\n".join(self.data)
        self.data.clear()
        self.data_size = 0
        if text.strip() == "[DONE]":
            self.done = True
            return
        try:
            event = json.loads(text)
            self.consume(event)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            self.error("invalid_sse_event:" + type(exc).__name__)

    def consume(self, event: dict[str, Any]) -> None:
        try:
            if event.get("error") is not None:
                self.error("upstream_sse_error")
                return
            for key in ("id", "model", "created", "usage"):
                if event.get(key) is not None:
                    self.info[key] = event[key]
            for delta_choice in event.get("choices", []):
                index = int(delta_choice["index"])
                state = self.choices.setdefault(index, {
                    "index": index, "message": {"role": "assistant"},
                    "finish_reason": None, "calls": {},
                })
                message = state["message"]
                delta = delta_choice.get("delta") or {}
                if delta.get("role"):
                    message["role"] = delta["role"]
                for key in ("content", "reasoning_content", "reasoning", "refusal"):
                    if delta.get(key) is not None:
                        if not isinstance(delta[key], str):
                            raise ValueError("non-text streamed assistant delta")
                        message[key] = message.get(key, "") + delta[key]
                if delta.get("function_call"):
                    legacy = message.setdefault("function_call", {"name": "", "arguments": ""})
                    for key in ("name", "arguments"):
                        legacy[key] += delta["function_call"].get(key, "")
                for call in delta.get("tool_calls") or []:
                    call_index = int(call["index"])
                    entry = state["calls"].setdefault(call_index, {
                        "id": "", "type": "function", "function": {"name": "", "arguments": ""},
                    })
                    if call.get("id") and call["id"] != entry["id"]:
                        entry["id"] += call["id"]
                    if call.get("type"):
                        entry["type"] = call["type"]
                    function = call.get("function") or {}
                    for key in ("name", "arguments"):
                        if function.get(key) is not None:
                            entry["function"][key] += function[key]
                if delta_choice.get("finish_reason") is not None:
                    state["finish_reason"] = delta_choice["finish_reason"]
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            self.error("invalid_sse_event:" + type(exc).__name__)

    def result(self) -> dict[str, Any]:
        choices = []
        for index in sorted(self.choices):
            state = self.choices[index]
            message = copy.deepcopy(state["message"])
            if state["calls"]:
                message["tool_calls"] = [state["calls"][key] for key in sorted(state["calls"])]
            choices.append({"index": index, "message": message, "finish_reason": state["finish_reason"]})
        return {**self.info, "choices": choices}


class ResponsesSSECollector(SSECollector):
    """Responses typed events, indexed output items and function-argument deltas."""

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[int, dict] = {}
        self.terminal = False
        self.final_output_supplied = False
        self.done_items: set[int] = set()

    def consume(self, event: dict[str, Any]) -> None:
        kind = event.get("type", "")
        if kind in {"error", "response.error"}:
            self.error("upstream_sse_error")
            return
        if kind in {"response.created", "response.in_progress", "response.queued",
                    "response.completed", "response.failed", "response.incomplete", "response.cancelled"}:
            response = event.get("response")
            if not isinstance(response, dict):
                raise ValueError("missing_response_object")
            self.info.update(copy.deepcopy(response))
            if kind in {"response.completed", "response.failed", "response.incomplete", "response.cancelled"}:
                if response.get("status") not in (None, kind.removeprefix("response.")):
                    raise ValueError("terminal_status_mismatch")
                self.terminal = True
                self.final_output_supplied = "output" in response
                if not self.final_output_supplied and set(self.items) != self.done_items:
                    self.error("missing_final_output_items")
                # The terminal response is authoritative. No double-appending of deltas.
                self.info["status"] = kind.removeprefix("response.")
            return
        if kind == "response.output_item.added":
            self.items[int(event["output_index"])] = copy.deepcopy(event["item"])
        elif kind == "response.output_item.done":
            index = int(event["output_index"])
            self.items[index] = copy.deepcopy(event["item"])
            self.done_items.add(index)
        elif kind.startswith("response.function_call_arguments."):
            item = self.items[int(event["output_index"])]
            if kind.endswith(".delta"):
                item["arguments"] = item.get("arguments", "") + event["delta"]
            elif kind.endswith(".done"):
                item["arguments"] = event["arguments"]
        elif kind in {"response.content_part.added", "response.content_part.done"}:
            item = self.items[int(event["output_index"])]
            parts = item.setdefault("content", [])
            index = int(event["content_index"])
            while len(parts) <= index:
                parts.append({})
            parts[index] = copy.deepcopy(event["part"])
        elif kind in {"response.output_text.delta", "response.output_text.done", "response.refusal.delta", "response.refusal.done"}:
            item = self.items[int(event["output_index"])]
            parts = item.setdefault("content", [])
            index = int(event["content_index"])
            while len(parts) <= index:
                parts.append({})
            refusal = ".refusal." in kind
            key = "refusal" if refusal else "text"
            part = parts[index]
            part.setdefault("type", "refusal" if refusal else "output_text")
            if kind.endswith(".delta"):
                part[key] = part.get(key, "") + event["delta"]
            else:
                part[key] = event[key]
        elif kind in {"response.reasoning_summary_part.added", "response.reasoning_summary_part.done"}:
            item = self.items[int(event["output_index"])]
            parts = item.setdefault("summary", [])
            index = int(event["summary_index"])
            while len(parts) <= index:
                parts.append({})
            parts[index] = copy.deepcopy(event["part"])
        elif kind in {"response.reasoning_summary_text.delta", "response.reasoning_summary_text.done"}:
            item = self.items[int(event["output_index"])]
            parts = item.setdefault("summary", [])
            index = int(event["summary_index"])
            while len(parts) <= index:
                parts.append({"type": "summary_text", "text": ""})
            if kind.endswith(".delta"):
                parts[index]["text"] = parts[index].get("text", "") + event["delta"]
            else:
                parts[index]["text"] = event["text"]
        # Unknown events remain in raw SSE. Final output preserves unknown native item types.

    def result(self) -> dict[str, Any]:
        result = copy.deepcopy(self.info)
        if not self.terminal or not self.final_output_supplied:
            result["output"] = [copy.deepcopy(self.items[index]) for index in sorted(self.items)]
        return result


def normalize_message(original: dict[str, Any], include_reasoning: bool) -> dict[str, Any]:
    message = copy.deepcopy(original)
    if not include_reasoning:
        message.pop("reasoning_content", None)
        message.pop("reasoning", None)
    if message.get("function_call") or message.get("role") == "function":
        raise ValueError("legacy_function_call_requires_conversion")
    if message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
        raise ValueError("unsupported_message_role")
    for call in message.get("tool_calls") or []:
        if call.get("type", "function") != "function":
            raise ValueError("unsupported_tool_call_type")
        if not call.get("id"):
            raise ValueError("missing_tool_call_id")
        function = call["function"]
        if not isinstance(function.get("name"), str) or not function["name"]:
            raise ValueError("missing_tool_name")
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not isinstance(arguments, dict):
            raise ValueError("tool_arguments_must_be_object")
        function["arguments"] = arguments
    return message


def make_samples(request: dict[str, Any], record: dict[str, Any], *,
                 include_reasoning: bool = False, output_format: str = "prompt-completion"
                 ) -> tuple[list[dict[str, Any]], list[str]]:
    reasons: list[str] = []
    if record.get("state") != "complete":
        return [], ["transport_incomplete"]
    if not 200 <= (record.get("upstream_status") or 0) < 300:
        return [], ["upstream_http_error"]
    if record.get("parse_errors"):
        return [], ["response_parse_error"]
    response = record.get("response") or {}
    if not isinstance(response, dict) or response.get("error") is not None:
        return [], ["upstream_error"]
    try:
        history = request["messages"]
        if not isinstance(history, list) or not history:
            return [], ["missing_messages"]
        history = [normalize_message(message, include_reasoning) for message in history]
        tools = request.get("tools") or []
        if not isinstance(tools, list):
            return [], ["invalid_tools"]
        definitions = {}
        for tool in tools:
            if tool.get("type") != "function":
                return [], ["unsupported_tool_type"]
            function = tool["function"]
            definitions[function["name"]] = function
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return [], ["invalid_request_history:" + str(exc)[:120]]

    samples: list[dict[str, Any]] = []
    for choice in response.get("choices", []):
        try:
            if choice.get("finish_reason") not in ("stop", "tool_calls"):
                reasons.append("unfinished_or_filtered_choice")
                continue
            message = normalize_message(choice["message"], include_reasoning)
            if message.get("role") != "assistant":
                raise ValueError("target_must_be_assistant")
            if not message.get("content") and not message.get("tool_calls"):
                raise ValueError("empty_assistant_target")
            for call in message.get("tool_calls") or []:
                function = call["function"]
                if function["name"] not in definitions:
                    raise ValueError("unknown_tool_name")
                schema = definitions[function["name"]].get("parameters", {"type": "object"})
                validator = validator_for(schema)
                validator.check_schema(schema)
                validator(schema).validate(function["arguments"])
            if output_format == "messages":
                sample = {"messages": history + [message]}
            else:
                sample = {"prompt": history, "completion": [message]}
            if "tools" in request:
                sample["tools"] = copy.deepcopy(tools)
            sample["metadata"] = {
                "request_id": record["request_id"], "choice_index": choice.get("index", 0),
                "captured_at": record.get("started_at"),
                "model": response.get("model") or request.get("model"),
                "session_id": record.get("session_id"), "task_id": record.get("task_id"),
                "tool_choice": request.get("tool_choice"),
                "parallel_tool_calls": request.get("parallel_tool_calls"),
                "business_verified": False,
            }
            samples.append(sample)
        except Exception as exc:
            # A malformed tool schema or argument excludes only this target.
            reasons.append("invalid_target:" + type(exc).__name__ + ":" + str(exc)[:160])
    if not response.get("choices"):
        reasons.append("missing_choices")
    return samples, reasons


class Capture:
    def __init__(self, settings: Settings, body: bytes, payload: dict[str, Any], headers: Any,
                 history: ResponseHistory | None = None, operation: str = "chat", method: str = "POST", path: str = ""):
        self.settings = settings
        self.payload = payload
        now = timestamp()
        self.record: dict[str, Any] = {
            "schema_version": 2, "request_id": uuid.uuid4().hex,
            "operation": operation, "method": method, "path": path,
            "history_scope": hashlib.sha256((settings.upstream + "\0" + settings.upstream_key).encode()).hexdigest(),
            "started_at": now, "state": "running", "upstream_status": None,
            "session_id": headers.get("x-session-id", "")[:256] or None,
            "task_id": headers.get("x-task-id", "")[:256] or None,
        }
        self.directory = settings.data_dir / "raw" / now[:10] / self.record["request_id"]
        self.directory.mkdir(parents=True, mode=0o700)
        with (self.directory / "request.json").open("wb") as request_file:
            request_file.write(body)
            request_file.flush()
            os.fsync(request_file.fileno())
        atomic_json(self.directory / "record.json", self.record)
        self.handle = (self.directory / "response.body").open("wb")
        self.parser: SSECollector | None = None
        self.upstream: httpx.Response | None = None
        self.body_complete = False
        self.finished = False
        self.start = __import__("time").monotonic()
        self.history = history
        self.checkpoint_signature: tuple | None = None

    def checkpoint(self) -> None:
        """Publish complete protocol events BEFORE forwarding them to the caller."""
        if not self.record["operation"].startswith("responses."):
            return
        if isinstance(self.parser, ResponsesSSECollector):
            response = self.parser.result()
            protocol_completed = self.parser.terminal and response.get("status") == "completed"
            errors = self.parser.errors
        elif self.body_complete:
            self.handle.flush()
            try:
                response = json.loads((self.directory / "response.body").read_bytes())
            except (ValueError, UnicodeError):
                return
            protocol_completed = isinstance(response, dict) and response.get("status") == "completed"
            errors = []
        else:
            return
        if not isinstance(response, dict) or not response.get("id"):
            return
        signature = (response["id"], protocol_completed)
        if signature == self.checkpoint_signature:
            return
        self.record.update(response=response, protocol_completed=protocol_completed, parse_errors=errors)
        atomic_json(self.directory / "record.json", self.record)
        if self.history is not None:
            self.history.publish(self.directory, self.record)
        self.checkpoint_signature = signature

    def write(self, chunk: bytes) -> None:
        self.handle.write(chunk)

    async def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        try:
            if self.upstream is not None:
                await self.upstream.aclose()
        finally:
            await asyncio.to_thread(self.save)

    def save(self) -> None:
        try:
            self.handle.flush()
            os.fsync(self.handle.fileno())
        finally:
            self.handle.close()
        self.record["finished_at"] = timestamp()
        self.record["elapsed_seconds"] = round(__import__("time").monotonic() - self.start, 3)
        self.record["response_bytes"] = (self.directory / "response.body").stat().st_size
        self.record["state"] = "complete" if self.body_complete else "incomplete"
        if self.parser is not None:
            self.record["response"] = self.parser.result()
            self.record["parse_errors"] = self.parser.errors
            self.record["sse_done_received"] = self.parser.done
            if isinstance(self.parser, ResponsesSSECollector):
                self.record["protocol_completed"] = self.parser.terminal and self.record["response"].get("status") == "completed"
                if not self.parser.terminal:
                    self.record["parse_errors"] = self.parser.errors + ["missing_responses_terminal_event"]
        else:
            try:
                self.record["response"] = json.loads((self.directory / "response.body").read_bytes())
            except (ValueError, UnicodeError):
                self.record["parse_errors"] = ["invalid_json_response"]
            response = self.record.get("response")
            self.record["protocol_completed"] = (
                self.body_complete and isinstance(response, dict) and response.get("status") == "completed")
        atomic_json(self.directory / "record.json", self.record)
        native: list[dict] = []
        if self.record["operation"].startswith("responses."):
            try:
                self.history.publish(self.directory, self.record)
                samples, native, reasons = build_response_exports(self.payload, self.record, self.history,
                    make_samples, include_reasoning=self.settings.include_reasoning)
            except Exception as exc:
                samples, reasons = [], ["history_error:" + type(exc).__name__ + ":" + str(exc)[:160]]
        else:
            samples, reasons = make_samples(
                self.payload, self.record, include_reasoning=self.settings.include_reasoning,
            )
        self.record["candidate_count"] = len(samples)
        self.record["native_count"] = len(native)
        self.record["skip_reasons"] = reasons
        # Persist completed raw capture first. Offline export can recover if appending fails.
        atomic_json(self.directory / "record.json", self.record)
        for prefix, rows in (("sft", samples), ("responses-native", native)):
            if not rows:
                continue
            destination = self.settings.data_dir / "datasets" / (prefix + "-" + self.record["started_at"][:10] + ".jsonl")
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                with FileLock(str(destination) + ".lock", timeout=60):
                    with destination.open("a", encoding="utf-8", newline="\n") as handle:
                        for sample in rows:
                            handle.write(dump(sample) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                self.record.setdefault("auto_exports", {})[prefix] = str(destination.relative_to(self.settings.data_dir))
                atomic_json(self.directory / "record.json", self.record)
            except Exception:
                self.record["export_error"] = True
                atomic_json(self.directory / "record.json", self.record)
                LOG.exception("Dataset append failed request_id=%s; raw capture retained", self.record["request_id"])
        LOG.info("captured request_id=%s state=%s candidates=%d", self.record["request_id"], self.record["state"], len(samples))


class RecordedStreamingResponse(StreamingResponse):
    def __init__(self, *args: Any, capture: Capture, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.capture = capture

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Client disconnect cancellation must not cancel the disk finalizer.
            with anyio.CancelScope(shield=True):
                await self.capture.finish()


def response_headers(response: httpx.Response, request_id: str | None = None) -> dict[str, str]:
    # HTTPX aiter_bytes decodes Content-Encoding. Never forward stale lengths/encoding.
    headers = {key: value for key, value in response.headers.items() if key.lower() in {
        "content-type", "cache-control", "retry-after", "x-request-id",
        "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
    }}
    headers["x-accel-buffering"] = "no"
    if request_id:
        headers["x-collector-request-id"] = request_id
    return headers


def create_app(settings: Settings, transport: Any = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        app.state.history = await asyncio.to_thread(ResponseHistory, settings.data_dir)
        await asyncio.to_thread(app.state.history.rebuild)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.read_timeout, connect=15.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            transport=transport, trust_env=False,
        ) as client:
            app.state.client = client
            yield

    app = FastAPI(title="Qwen dataset capture proxy", lifespan=lifespan, docs_url=None, redoc_url=None)

    def authenticate(request: Request) -> None:
        if settings.proxy_key and not hmac.compare_digest(
            request.headers.get("authorization", ""), "Bearer " + settings.proxy_key,
        ):
            raise HTTPException(401, "Invalid proxy API key")

    def upstream_headers() -> dict[str, str]:
        headers = {"content-type": "application/json", "accept-encoding": "identity"}
        if settings.upstream_key:
            headers["authorization"] = "Bearer " + settings.upstream_key
        return headers

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        authenticate(request)
        try:
            response = await app.state.client.get(settings.upstream + "/models", headers=upstream_headers())
            return Response(response.content, status_code=response.status_code, headers=response_headers(response))
        except httpx.HTTPError:
            raise HTTPException(502, "Upstream model service unavailable")

    async def forward(request: Request, operation: str, suffix: str) -> Response:
        authenticate(request)
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > settings.max_request_bytes:
                raise HTTPException(413, "Request body too large")
        raw_body = bytes(body)
        try:
            payload = json.loads(raw_body) if raw_body else {}
            if not isinstance(payload, dict):
                raise ValueError("request must be object")
            if operation == "chat" and not isinstance(payload.get("messages"), list):
                raise ValueError("missing messages array")
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(400, "Invalid OpenAI-compatible JSON body") from exc
        try:
            capture = await asyncio.to_thread(Capture, settings, raw_body, payload, request.headers,
                                             app.state.history, operation, request.method, request.url.path)
        except OSError as exc:
            LOG.error("Cannot initialize raw capture: %s", type(exc).__name__)
            raise HTTPException(507, "Capture storage unavailable") from exc
        try:
            upstream_request = app.state.client.build_request(
                request.method, settings.upstream + suffix,
                params=request.query_params, headers=upstream_headers(), content=raw_body,
            )
            response = await app.state.client.send(upstream_request, stream=True)
            capture.upstream = response
            capture.record["upstream_status"] = response.status_code
            capture.record["response_content_type"] = response.headers.get("content-type")
            is_sse = "text/event-stream" in response.headers.get("content-type", "").lower()
            if is_sse:
                capture.parser = ResponsesSSECollector() if operation.startswith("responses.") else SSECollector()
        except BaseException as exc:
            capture.record["transport_error"] = type(exc).__name__
            with anyio.CancelScope(shield=True):
                await capture.finish()
            if isinstance(exc, httpx.TimeoutException):
                raise HTTPException(504, "Upstream model service timed out") from exc
            if isinstance(exc, httpx.HTTPError):
                raise HTTPException(502, "Upstream model service unavailable") from exc
            raise

        async def stream():
            try:
                async for chunk in response.aiter_bytes():
                    await asyncio.to_thread(capture.write, chunk)
                    if capture.parser is not None:
                        capture.parser.feed(chunk)
                        if isinstance(capture.parser, ResponsesSSECollector):
                            # Parsing is cheap; disk checkpoints only happen once per ID/terminal transition.
                            signature = (capture.parser.info.get("id"), capture.parser.terminal and
                                         capture.parser.info.get("status") == "completed")
                            if signature != capture.checkpoint_signature and signature[0]:
                                await asyncio.to_thread(capture.checkpoint)
                    yield chunk
                if capture.parser is not None:
                    capture.parser.feed(b"", final=True)
                capture.body_complete = True
                if operation.startswith("responses."):
                    await asyncio.to_thread(capture.checkpoint)
            except BaseException as exc:
                capture.record["transport_error"] = type(exc).__name__
                raise

        return RecordedStreamingResponse(
            stream(), status_code=response.status_code,
            headers=response_headers(response, capture.record["request_id"]), capture=capture,
        )

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Response:
        return await forward(request, "chat", "/chat/completions")

    @app.post("/v1/responses")
    async def responses(request: Request) -> Response:
        return await forward(request, "responses.create", "/responses")

    @app.api_route("/v1/responses/{response_id}", methods=["GET", "DELETE"])
    async def response_lifecycle(response_id: str, request: Request) -> Response:
        from urllib.parse import quote
        operation = "responses.retrieve" if request.method == "GET" else "responses.delete"
        return await forward(request, operation, "/responses/" + quote(response_id, safe=""))

    @app.post("/v1/responses/{response_id}/cancel")
    async def response_cancel(response_id: str, request: Request) -> Response:
        from urllib.parse import quote
        return await forward(request, "responses.cancel", "/responses/" + quote(response_id, safe="") + "/cancel")

    return app


def export_dataset(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir).resolve()
    output = Path(args.output).resolve()
    if not (data_dir / "raw").is_dir():
        raise ValueError("找不到 data-dir 下的 raw/ 目录，请检查采集数据路径")
    if output.exists() and not args.overwrite:
        raise ValueError("输出已存在；请换一个文件名或加 --overwrite")
    if output == data_dir or data_dir / "raw" in output.parents or data_dir / "datasets" in output.parents:
        raise ValueError("离线导出请使用 raw/ 和 datasets/ 之外的路径，避免覆盖正在采集的数据")
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + "." + uuid.uuid4().hex + ".tmp")
    counts: Counter = Counter()
    seen: set[str] = set()
    history = ResponseHistory(data_dir)
    history.rebuild()
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            for record_path in sorted((data_dir / "raw").glob("*/*/record.json")):
                counts["records"] += 1
                try:
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                    body = record_path.with_name("request.json").read_bytes()
                    request = json.loads(body) if body else {}
                    if record.get("operation", "chat").startswith("responses."):
                        samples, native, reasons = build_response_exports(request, record, history,
                            make_samples, include_reasoning=args.include_reasoning,
                            output_format="messages" if args.format == "messages" else "prompt-completion")
                        if args.format == "responses-native":
                            samples = native
                            if native:
                                reasons = []
                    elif args.format == "responses-native":
                        samples, reasons = [], ["not_responses_record"]
                    else:
                        samples, reasons = make_samples(request, record,
                            include_reasoning=args.include_reasoning, output_format=args.format)
                except Exception:
                    counts["unreadable_records"] += 1
                    continue
                for reason in reasons:
                    counts["skip:" + reason.split(":", 1)[0]] += 1
                for sample in samples:
                    canonical = {key: value for key, value in sample.items() if key != "metadata"}
                    digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                    if not args.keep_duplicates and digest in seen:
                        counts["duplicates"] += 1
                        continue
                    seen.add(digest)
                    handle.write(dump(sample) + "\n")
                    counts["samples"] += 1
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)
    summary = {"output": str(output), "format": args.format, "counts": dict(counts)}
    atomic_json(output.with_suffix(output.suffix + ".stats.json"), summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="运行采集代理")
    serve.add_argument("--upstream", default=os.getenv("UPSTREAM_BASE_URL", "http://127.0.0.1:8000/v1"))
    serve.add_argument("--data-dir", default=os.getenv("COLLECTOR_DATA_DIR", "./data"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=9000)
    serve.add_argument("--read-timeout", type=float, default=600)
    serve.add_argument("--max-request-mb", type=int, default=32)
    serve.add_argument("--include-reasoning", action="store_true", help="自动导出也保留独立 reasoning 字段")
    export = commands.add_parser("export", help="从原始记录重新导出（默认去重）")
    export.add_argument("--data-dir", required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--format", choices=["prompt-completion", "messages", "responses-native"], default="prompt-completion")
    export.add_argument("--include-reasoning", action="store_true")
    export.add_argument("--keep-duplicates", action="store_true")
    export.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "export":
        return export_dataset(args)
    if args.read_timeout <= 0 or args.max_request_mb <= 0:
        parser.error("timeout 和 max-request-mb 必须大于 0")
    settings = Settings(
        upstream=args.upstream, data_dir=Path(args.data_dir),
        proxy_key=os.getenv("PROXY_API_KEY", ""), upstream_key=os.getenv("UPSTREAM_API_KEY", ""),
        read_timeout=args.read_timeout, max_request_bytes=args.max_request_mb * 1024 * 1024,
        include_reasoning=args.include_reasoning,
    )
    if args.host not in ("127.0.0.1", "::1", "localhost") and not settings.proxy_key:
        parser.error("监听非本地地址时，请先设置环境变量 PROXY_API_KEY")
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info", timeout_graceful_shutdown=30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
