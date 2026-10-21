#!/usr/bin/env python3
"""Batch questions through Wing's Agent runtime API, not the model API. Python >=3.10."""
from __future__ import annotations

import argparse
import asyncio
import base64
import codecs
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import quote, urlsplit
import uuid

import httpx
from filelock import FileLock


def now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load_questions(path: Path) -> list[dict]:
    items, seen = [], set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, str):
            item = {"question": item}
        if not isinstance(item, dict):
            raise ValueError(f"第 {line_number} 行必须为 JSON 对象或字符串")
        turns = item.get("turns", [item.get("question")])
        if not isinstance(turns, list) or not turns or any(not isinstance(t, str) or not t.strip() for t in turns):
            raise ValueError(f"第 {line_number} 行需要 question 字符串或非空 turns 字符串数组")
        item_id = str(item.get("id", f"line-{line_number}"))
        if item_id in seen:
            raise ValueError(f"重复题目 ID: {item_id}")
        seen.add(item_id)
        items.append({"id": item_id, "turns": turns})
    if not items:
        raise ValueError("题目文件为空")
    return items


class SSE:
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.buffer, self.name, self.data = "", "", []
        self.size = 0

    def feed(self, chunk: bytes, final: bool = False) -> list[dict]:
        self.buffer += self.decoder.decode(chunk, final=final)
        events = []
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.line(line.rstrip("\r"), events)
        if len(self.buffer) > 8 * 1024 * 1024:
            raise ValueError("SSE 单行超过 8 MiB")
        if final:
            if self.buffer:
                self.line(self.buffer.rstrip("\r"), events)
                self.buffer = ""
            self.line("", events)
        return events

    def line(self, line: str, events: list[dict]) -> None:
        if line.startswith("data:"):
            self.data.append(line[5:].removeprefix(" "))
            self.size += len(line)
            if self.size > 8 * 1024 * 1024:
                raise ValueError("SSE 单事件超过 8 MiB")
        elif line.startswith("event:"):
            self.name = line[6:].strip()
        elif not line:
            if self.data:
                text = "\n".join(self.data)
                if text.strip() == "[DONE]":
                    event = {"type": "done"}
                else:
                    try:
                        event = json.loads(text)
                    except ValueError:
                        event = {"type": "delta", "content": text}
                if not isinstance(event, dict):
                    raise ValueError("SSE data 必须为对象")
                if not event.get("type") and self.name:
                    event["type"] = self.name
                events.append(event)
            self.data, self.name, self.size = [], "", 0


class Auth:
    def __init__(self, args, client):
        self.args, self.client = args, client
        self.token = os.getenv("WING_TOKEN", "").strip().removeprefix("Bearer ").strip()
        self.tenant_id = args.tenant_id
        self.user_id = args.user_id

    def headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self.token, "X-Tenant-ID": self.tenant_id,
                "Content-Type": "application/json", "Accept": "text/event-stream"}

    def adopt(self, session: dict) -> None:
        token = session.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("登录/刷新响应缺少 token")
        self.token = token
        self.tenant_id = str((session.get("tenant") or {}).get("id") or self.tenant_id)
        self.user_id = str((session.get("user") or {}).get("id") or self.user_id)

    async def initialize(self):
        if self.args.login:
            tenant_code = self.args.tenant_code or input("平台租户编码: ").strip()
            username = self.args.username or input("平台用户名: ").strip()
            password = os.getenv("WING_PASSWORD") or getpass.getpass("平台登录密码: ")
            response = await self.client.post(self.args.control_base + "/api/auth/login", json={
                "tenant_code": tenant_code, "username": username, "password": password,
            })
            password = ""
            response.raise_for_status()
            self.adopt(response.json())
        if not self.token:
            raise ValueError("请用 --login 登录，或设置环境变量 WING_TOKEN 和 WING_TENANT_ID")
        if not self.tenant_id:
            raise ValueError("缺少租户 ID：请设置 WING_TENANT_ID，或使用 --login 自动取得")

    async def refresh(self):
        # Decode ONLY the expiry hint; authentication is always enforced by the server.
        try:
            segment = self.token.split(".")[1]
            exp = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))).get("exp")
        except (ValueError, IndexError, TypeError, AttributeError):
            return
        if isinstance(exp, (int, float)) and exp <= time.time() + 300:
            response = await self.client.post(self.args.control_base + "/api/auth/refresh",
                                               headers={"Authorization": "Bearer " + self.token})
            response.raise_for_status()
            self.adopt(response.json())


async def send_turn(args, client, auth, mode, agent_code, question, session_id, directory):
    directory.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid.uuid4())
    if mode == "agui":
        url = args.gateway_base + "/api/gateway/" + quote(agent_code, safe="") + "/agui/run"
        payload = {"threadId": session_id, "runId": run_id,
                   "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": question}],
                   "userKey": auth.token, "tenantId": auth.tenant_id, "userId": auth.user_id}
    else:
        url = args.control_base + "/api/admin/agent-configs/" + quote(args.agent_id, safe="") + "/debug-chat/stream"
        payload = {"message": question, "session_id": session_id}
    stored = dict(payload)
    if "userKey" in stored:
        stored["userKey"] = "[REDACTED]"
    save(directory / "request.json", {"url": url, "body": stored})
    result = {"status": "running", "started_at": now(), "question": question, "session_id": session_id,
              "run_id": run_id, "mode": mode, "content": "", "reasoning_content": "",
              "tool_events": [], "errors": [], "terminal_received": False, "business_verified": False}
    save(directory / "result.json", result)
    parser = SSE()

    def consume(event, events_file):
        events_file.write(encoded(event) + "\n")
        events_file.flush()
        kind = event.get("type")
        if kind in {"delta", "TEXT_MESSAGE_CONTENT"}:
            result["content"] += (event.get("content") or "") if kind == "delta" else (event.get("delta") or "")
        if kind == "delta":
            result["reasoning_content"] += event.get("reasoning_content") or ""
        elif kind == "REASONING_MESSAGE_CONTENT":
            result["reasoning_content"] += event.get("delta") or ""
        if kind == "tool_event" or (isinstance(kind, str) and kind.startswith("TOOL_CALL_")):
            result["tool_events"].append(event)
        if kind in {"error", "RUN_ERROR"}:
            result["errors"].append(event)
            result["terminal_received"] = True
        if kind == "done" and mode == "debug":
            result["terminal_received"] = True
            if event.get("finish_reason") in {"length", "content_filter"}:
                result["errors"].append({"message": "unfinished_or_filtered", "event": event})
            result["done_event"] = event
        if kind == "RUN_FINISHED" and mode == "agui":
            result["terminal_received"] = True
            result["outcome"] = event.get("outcome", "success")
            result["done_event"] = event

    async def receive():
        async with client.stream("POST", url, json=payload, headers=auth.headers()) as response:
            result["http_status"] = response.status_code
            with (directory / "response.body").open("wb") as raw, \
                    (directory / "events.jsonl").open("w", encoding="utf-8", newline="\n") as events_file:
                if not response.is_success:
                    async for chunk in response.aiter_bytes():
                        raw.write(chunk)
                        raw.flush()
                    response.raise_for_status()
                if "text/event-stream" not in response.headers.get("content-type", "").lower():
                    async for chunk in response.aiter_bytes():
                        raw.write(chunk)
                        raw.flush()
                    raise ValueError("接口没有返回 SSE，请检查平台地址、登录状态和反向代理")
                async for chunk in response.aiter_bytes():
                    raw.write(chunk)
                    raw.flush()
                    for event in parser.feed(chunk):
                        consume(event, events_file)
                    if result["terminal_received"]:
                        break
                for event in parser.feed(b"", final=True):
                    consume(event, events_file)
                raw.flush()
                os.fsync(raw.fileno())
                events_file.flush()
                os.fsync(events_file.fileno())

    try:
        await asyncio.wait_for(receive(), timeout=args.task_timeout)
        if result.get("outcome") == "interrupt":
            result["status"] = "needs_action"
        elif result["errors"] or result.get("outcome", "success") not in {"success", "completed"}:
            result["status"] = "failed"
        elif not result["terminal_received"]:
            result["status"] = "incomplete"
        else:
            result["status"] = "success"
    except httpx.HTTPStatusError as exc:
        result.update(status="failed", error_type=type(exc).__name__)
        if exc.response.status_code in {401, 403}:
            raise
    except (asyncio.CancelledError, KeyboardInterrupt):
        result["status"] = "interrupted"
        raise
    except Exception as exc:
        # Do not serialize exceptions: an HTTP request body may contain userKey.
        result.update(status="incomplete", error_type=type(exc).__name__)
    finally:
        result["finished_at"] = now()
        save(directory / "result.json", result)
    return result


def rebuild_qa(root: Path) -> int:
    destination = root / "qa.jsonl"
    temp = root / ("qa." + uuid.uuid4().hex + ".tmp")
    count = 0
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            for path in sorted((root / "cases").glob("*/case.json")):
                case = json.loads(path.read_text(encoding="utf-8"))
                if case.get("status") != "success":
                    continue
                conversation = []
                for turn in case["results"]:
                    question = turn["question"]
                    handle.write(encoded({"prompt": conversation + [{"role": "user", "content": question}],
                        "completion": [{"role": "assistant", "content": turn["content"]}],
                        "metadata": {"case_key": case["case_key"], "question_id": case["question_id"],
                            "session_id": case["session_id"], "mode": case["mode"],
                            "business_verified": False, "source": turn["source"],
                            "dataset_kind": "agent_final_qa_without_tool_trace"}}) + "\n")
                    conversation.extend([{"role": "user", "content": question},
                                         {"role": "assistant", "content": turn["content"]}])
                    count += 1
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    return count


async def run(args):
    questions = load_questions(Path(args.input))
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / ".batch.lock"), timeout=0):
        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                    timeout=httpx.Timeout(args.read_timeout, connect=15)) as client:
                auth = Auth(args, client)
                await auth.initialize()
                await auth.refresh()
                response = await client.get(args.control_base + "/api/admin/agent-configs/" + quote(args.agent_id, safe=""),
                                            headers=auth.headers())
                response.raise_for_status()
                config = response.json()
                mode = args.mode
                if mode == "auto":
                    mode = "agui" if config.get("agui_enabled") and config.get("agent_code") else "debug"
                agent_code = config.get("agent_code") or ""
                if mode == "agui" and not agent_code:
                    raise ValueError("AG-UI 模式需要配置中的 agent_code")
                print(f"模式: {mode}; 题目数: {len(questions)}; 输出: {root}", flush=True)
                processed = skipped = failed = 0
                for item in questions:
                    for repeat in range(args.repeat):
                        identity = {"control": args.control_base, "gateway": args.gateway_base,
                                    "agent": args.agent_id, "tenant": auth.tenant_id, "mode": mode,
                                    "item": item, "repeat": repeat}
                        key = hashlib.sha256(encoded(identity).encode()).hexdigest()
                        case_dir = root / "cases" / key
                        case_file = case_dir / "case.json"
                        if case_file.exists():
                            previous = json.loads(case_file.read_text(encoding="utf-8"))
                            if previous.get("status") == "success" or not args.retry_failed:
                                skipped += 1
                                continue
                        if args.limit and processed >= args.limit:
                            break
                        processed += 1
                        session_id = str(uuid.uuid4())
                        attempt = uuid.uuid4().hex
                        case = {"case_key": key, "question_id": item["id"], "repeat": repeat + 1,
                                "session_id": session_id, "attempt_id": attempt, "mode": mode,
                                "agent_id": args.agent_id, "started_at": now(), "status": "running", "results": []}
                        save(case_file, case)
                        print(f"[{processed}] {item['id']} 第{repeat + 1}次，会话 {session_id}", flush=True)
                        try:
                            for index, question in enumerate(item["turns"], 1):
                                await auth.refresh()
                                directory = case_dir / "attempts" / attempt / f"turn-{index:03d}"
                                turn = await send_turn(args, client, auth, mode, agent_code, question, session_id, directory)
                                turn["source"] = directory.relative_to(root).as_posix()
                                case["results"].append(turn)
                                case["status"] = turn["status"] if turn["status"] != "success" else "running"
                                save(case_file, case)
                                if turn["status"] != "success":
                                    break
                            if len(case["results"]) == len(item["turns"]) and all(t["status"] == "success" for t in case["results"]):
                                case["status"] = "success"
                        except BaseException as exc:
                            case.update(status="interrupted" if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)) else "incomplete",
                                        error_type=type(exc).__name__)
                            raise
                        finally:
                            case["finished_at"] = now()
                            save(case_file, case)
                        print(f"  {case['status']}", flush=True)
                        if case["status"] != "success":
                            failed += 1
                        await asyncio.sleep(args.interval)
                    if args.limit and processed >= args.limit:
                        break
                print(f"本次完成: {processed - failed}; 未完成: {failed}; 跳过已有: {skipped}", flush=True)
                return 1 if failed else 0
        finally:
            count = rebuild_qa(root)
            print(f"已导出 QA: {root / 'qa.jsonl'}，共 {count} 条；工具训练集请使用采集代理的 data/datasets。", flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-url", required=True, help="数字员工 /agent-runtime/<id> 页面地址")
    parser.add_argument("--input", required=True, help="问题 JSONL 文件")
    parser.add_argument("--output", default="data/batch", help="保存记录和断点的目录")
    parser.add_argument("--mode", choices=["auto", "debug", "agui"], default="auto")
    parser.add_argument("--control-base", help="默认与页面同源，可覆盖控制面地址")
    parser.add_argument("--gateway-base", help="默认与页面同源，可覆盖 AG-UI 网关地址")
    parser.add_argument("--tenant-id", default=os.getenv("WING_TENANT_ID", ""))
    parser.add_argument("--user-id", default=os.getenv("WING_USER_ID", ""))
    parser.add_argument("--login", action="store_true", help="使用平台账号登录，密码交互输入或 WING_PASSWORD")
    parser.add_argument("--tenant-code", default=os.getenv("WING_TENANT_CODE", ""))
    parser.add_argument("--username", default=os.getenv("WING_USERNAME", ""))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0, help="本次最多发送多少组题，0 为不限")
    parser.add_argument("--interval", type=float, default=1, help="每组题之间间隔秒数")
    parser.add_argument("--read-timeout", type=float, default=600)
    parser.add_argument("--task-timeout", type=float, default=1800, help="每轮总时间上限秒数")
    parser.add_argument("--retry-failed", action="store_true", help="显式重新提交未成功题组，可能重复执行工具")
    args = parser.parse_args()
    page = urlsplit(args.page_url)
    segments = [part for part in page.path.split("/") if part]
    if page.scheme not in {"http", "https"} or not page.netloc or page.username or page.password:
        parser.error("page-url 必须为不带凭据的 http(s) 页面地址")
    if len(segments) != 2 or segments[0] != "agent-runtime":
        parser.error("page-url 路径应为 /agent-runtime/<agentConfigId>")
    args.agent_id = segments[1]
    origin = f"{page.scheme}://{page.netloc}"
    args.control_base = (args.control_base or origin).rstrip("/")
    args.gateway_base = (args.gateway_base or origin).rstrip("/")
    for value in (args.control_base, args.gateway_base):
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password or url.query or url.fragment:
            parser.error("control-base/gateway-base 必须为不带凭据、query 或 fragment 的 http(s) 地址")
    if args.repeat < 1 or args.limit < 0 or args.interval < 0 or args.read_timeout <= 0 or args.task_timeout <= 0:
        parser.error("repeat/超时必须大于 0，limit/interval 不能为负数")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("已中断；已收到的响应与断点保留。", file=sys.stderr)
        return 130
    except ValueError as exc:
        print(f"参数或数据错误: {exc}", file=sys.stderr)
        return 2
    except httpx.HTTPStatusError as exc:
        print(f"平台接口返回 HTTP {exc.response.status_code}: {exc.request.url.path}；"
              "检查平台账号、租户和访问权限。", file=sys.stderr)
        return 2
    except Exception as exc:
        # Login and AG-UI bodies contain credentials, so never dump tracebacks.
        print(f"批量任务停止: {type(exc).__name__}。检查登录状态、网络或输出目录中的结果文件。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
