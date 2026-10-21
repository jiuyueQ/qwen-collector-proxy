#!/usr/bin/env python3
"""Ask questions by typing and clicking in the visible Wing runtime webpage."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
import uuid

from filelock import FileLock, Timeout as LockTimeout
from playwright.sync_api import sync_playwright

from batch_agent_questions import encoded, is_oom, load_questions, now, rebuild_qa, save

COMPOSER = ".agent-runtime-composer input"
SEND = "button.agent-runtime-composer__submit"
USER = "article.agent-runtime-message--user"
ASSISTANT = "article.agent-runtime-message--assistant"


class StopBatch(RuntimeError):
    pass


def ready(page, timeout=60000):
    page.locator(COMPOSER).wait_for(state="visible", timeout=timeout)
    page.locator(SEND).wait_for(state="visible", timeout=timeout)
    if not page.locator(COMPOSER).is_enabled() or "is-loading" in (page.locator(SEND).get_attribute("class") or ""):
        raise StopBatch("页面仍在生成，请等待当前任务结束后重启脚本。")


def screenshot(page, directory):
    try:
        page.screenshot(path=str(directory / "page.png"), full_page=True, timeout=15000)
    except Exception:
        pass


def read_assistant(page):
    message = page.locator(ASSISTANT).last
    text = "\n".join(message.locator(".agent-runtime-markdown").all_inner_texts())
    reasoning = "\n".join(message.locator(".agent-runtime-reasoning pre").all_inner_texts())
    tools = []
    for card in message.locator("details.agent-runtime-tool").all():
        tools.append({"class": card.get_attribute("class"), "text": card.text_content()})
    return {"content": text, "reasoning_content": reasoning, "tool_cards": tools,
            "visible_message": message.inner_text()}


def send_turn(page, question, directory, state, args):
    directory.mkdir(parents=True, exist_ok=True)
    result = {"question": question, "status": "running", "started_at": now(), "mode": "browser",
              "business_verified": False, "content": "", "session_id": None}
    save(directory / "question.json", {"question": question})
    save(directory / "result.json", result)
    pending = {"question": question, "sessions": [], "request_count": 0, "http_errors": []}
    state["pending"] = pending
    try:
        ready(page)
        users_before = page.locator(USER).count()
        assistants_before = page.locator(ASSISTANT).count()
        page.locator(COMPOSER).click()
        page.locator(COMPOSER).fill("")
        page.locator(COMPOSER).press_sequentially(question, delay=args.typing_delay_ms)
        page.locator(SEND).click()
        deadline = time.monotonic() + args.task_timeout
        stable_since, last_snapshot = None, None
        while time.monotonic() < deadline:
            if page.is_closed():
                raise StopBatch("浏览器页面已关闭。")
            if urlsplit(page.url).path != urlsplit(args.page_url).path:
                raise StopBatch("页面离开了指定运行页，请检查登录状态。")
            interrupt = page.get_by_role("dialog").filter(has_text="前端工具中断")
            if interrupt.count() and interrupt.first.is_visible():
                result["status"] = "needs_action"
                raise StopBatch("页面弹出前端工具中断，已暂停整批，请由你处理；脚本不填写模拟工具结果。")
            errors = page.locator(".el-message--error").all_inner_texts()
            if errors:
                result["page_errors"] = errors
                result["status"] = "failed"
                raise StopBatch("页面出现错误提示，已停止整批。")
            if pending["http_errors"]:
                result["http_errors"] = pending["http_errors"]
                result["status"] = "failed"
                raise StopBatch("页面提问请求返回 HTTP 错误，已停止整批。")
            answered = page.locator(USER).count() == users_before + 1 and page.locator(ASSISTANT).count() > assistants_before
            idle = (page.locator(COMPOSER).is_enabled()
                    and "is-loading" not in (page.locator(SEND).get_attribute("class") or ""))
            if answered and idle:
                snapshot = read_assistant(page)
                fingerprint = encoded(snapshot)
                if fingerprint != last_snapshot:
                    last_snapshot, stable_since = fingerprint, time.monotonic()
                elif stable_since is not None and time.monotonic() - stable_since >= 1.5:
                    result.update(snapshot)
                    result["status"] = "success"
                    break
            else:
                stable_since = None
            page.wait_for_timeout(300)
        else:
            result["status"] = "incomplete"
            raise StopBatch("等待回复超时，后端是否结束尚未确认，已停止整批。")
        sessions = set(pending["sessions"])
        if pending["request_count"] != 1 or len(sessions) != 1:
            result["status"] = "unverified_session"
            raise StopBatch("无法确认一次点击只发送了一次请求并使用唯一会话，已停止整批。")
        result["session_id"] = next(iter(sessions))
        result["observed_request_count"] = pending["request_count"]
        save(directory / "request_observation.json", {"session_id": result["session_id"],
             "request_count": pending["request_count"], "transport": pending.get("transport"),
             "source": "passively_observed_request_sent_by_webpage"})
        if is_oom(result.get("page_errors", [])) or is_oom(result.get("visible_message", "")):
            result["status"] = "oom_detected"
            raise StopBatch("页面出现显存不足信息，已停止整批。")
        if any("--error" in (tool.get("class") or "") for tool in result.get("tool_cards", [])):
            result["tool_error_detected"] = True
    except BaseException as exc:
        if result["status"] == "running":
            result["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "incomplete"
        result["error_type"] = type(exc).__name__
        raise
    finally:
        state["pending"] = None
        result["observed_sessions"] = pending["sessions"]
        result["finished_at"] = now()
        save(directory / "result.json", result)
        screenshot(page, directory)
    return result


def run(args):
    questions = load_questions(Path(args.input))
    for item in questions:
        if len(item["turns"]) > 3 or any(len(turn) > 4000 for turn in item["turns"]):
            raise ValueError("每组最多3轮，每轮问题最多4000字符。")
        if any("\n" in turn or "\r" in turn for turn in item["turns"]):
            raise ValueError("运行页是单行输入框，题目文本不能包含换行，以免意外触发发送。")
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    profile = Path(args.profile).resolve()
    profile.parent.mkdir(parents=True, exist_ok=True)
    observed_sessions = set()
    for path in (root / "cases").glob("*/case.json"):
        previous = json.loads(path.read_text(encoding="utf-8"))
        if previous.get("session_id"):
            observed_sessions.add(previous["session_id"])
    with FileLock(str(root / ".batch.lock"), timeout=0), FileLock(str(profile) + ".lock", timeout=0):
        context = None
        try:
            with sync_playwright() as playwright:
                options = {"headless": False, "viewport": {"width": 1440, "height": 1000}}
                if args.browser != "chromium":
                    options["channel"] = args.browser
                context = playwright.chromium.launch_persistent_context(str(profile), **options)
                page = context.pages[0] if context.pages else context.new_page()
                state = {"pending": None}

                def is_agent_request(request):
                    url = urlsplit(request.url)
                    return (request.method == "POST" and url.netloc == urlsplit(args.page_url).netloc
                            and (url.path.endswith("/debug-chat/stream") or url.path.endswith("/agui/run")))

                def on_request(request):
                    pending = state["pending"]
                    if not pending or not is_agent_request(request):
                        return
                    try:
                        body = request.post_data_json
                        messages = body.get("messages") or []
                        submitted = body.get("message") if "message" in body else (messages[-1].get("content") if messages else None)
                        if submitted != pending["question"]:
                            return
                        pending["request_count"] += 1
                        session = body.get("session_id") or body.get("threadId")
                        if isinstance(session, str) and session:
                            pending["sessions"].append(session)
                        pending["transport"] = "agui" if "threadId" in body else "debug"
                        # Do not save headers, userKey, passwords, tokens or raw bodies.
                    except (ValueError, TypeError, AttributeError):
                        pass

                def on_response(response):
                    if state["pending"] and is_agent_request(response.request) and response.status >= 400:
                        state["pending"]["http_errors"].append(response.status)

                page.on("request", on_request)
                page.on("response", on_response)
                page.goto(args.page_url, wait_until="domcontentloaded", timeout=60000)
                print("\n请在打开的浏览器中手动登录并进入数字员工运行页。", flush=True)
                print("平台记忆应按会话隔离，输出令牌数量不超过4096；模型服务保持单并发。", flush=True)
                input("准备好后回到这个窗口按回车，脚本才会开始输入问题：")
                if urlsplit(page.url).path != urlsplit(args.page_url).path:
                    page.goto(args.page_url, wait_until="domcontentloaded", timeout=60000)
                ready(page)
                processed = 0
                for item in questions:
                    for repeat in range(args.repeat):
                        if args.limit and processed >= args.limit:
                            break
                        identity = {"page": args.page_url, "mode": "browser", "id": item["id"],
                                    "turns": item["turns"], "repeat": repeat}
                        key = hashlib.sha256(encoded(identity).encode()).hexdigest()
                        case_file = root / "cases" / key / "case.json"
                        if case_file.exists():
                            continue
                        processed += 1
                        # A full navigation creates a fresh Vue component. The page's
                        # own createUuid() supplies a new session/thread ID.
                        page.goto(args.page_url, wait_until="domcontentloaded", timeout=60000)
                        ready(page)
                        if page.locator(USER).count():
                            raise StopBatch("重新打开页面后仍有用户消息，无法确认会话已重置。")
                        attempt = uuid.uuid4().hex
                        case = {"case_key": key, "question_id": item["id"], "attempt_id": attempt,
                                "mode": "browser", "status": "running", "started_at": now(), "session_id": None,
                                "question_metadata": item["metadata"], "results": []}
                        save(case_file, case)
                        print(f"[{processed}] {item['id']}：浏览器输入并发送", flush=True)
                        try:
                            for index, question in enumerate(item["turns"], 1):
                                directory = case_file.parent / "attempts" / attempt / f"turn-{index:03d}"
                                turn = send_turn(page, question, directory, state, args)
                                turn["source"] = directory.relative_to(root).as_posix()
                                case["results"].append(turn)
                                session = turn["session_id"]
                                if case["session_id"] is None:
                                    if session in observed_sessions:
                                        raise StopBatch("检测到不同题组复用了会话 ID，已停止。")
                                    case["session_id"] = session
                                    observed_sessions.add(session)
                                elif session != case["session_id"]:
                                    raise StopBatch("同组多轮的会话 ID 发生变化，已停止。")
                                save(case_file, case)
                                if turn.get("tool_error_detected") and index < len(item["turns"]):
                                    case["status"] = "stopped_on_tool_error"
                                    break
                            else:
                                case["status"] = "success"
                        except BaseException as exc:
                            case["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "stopped"
                            case["error_type"] = type(exc).__name__
                            raise
                        finally:
                            case["finished_at"] = now()
                            save(case_file, case)
                        print("  " + case["status"], flush=True)
                        page.wait_for_timeout(args.interval * 1000)
                    if args.limit and processed >= args.limit:
                        break
                print("本轮网页提问已结束。", flush=True)
                input("按回车关闭脚本启动的浏览器：")
                context.close()
                context = None
                return 0
        finally:
            count = rebuild_qa(root)
            print(f"问答已导出：{root / 'qa.jsonl'}（{count}条）；模型原始记录由服务器采集器保存。", flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-url", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", default="data/browser")
    parser.add_argument("--profile", default="browser_profiles/wing")
    parser.add_argument("--browser", choices=["msedge", "chrome", "chromium"], default="msedge")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--interval", type=float, default=3)
    parser.add_argument("--typing-delay-ms", type=int, default=35)
    parser.add_argument("--task-timeout", type=float, default=1800)
    args = parser.parse_args()
    url = urlsplit(args.page_url)
    if (url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password
            or not url.path.startswith("/agent-runtime/")):
        parser.error("请提供不带凭据的 /agent-runtime/<id> 页面地址")
    if args.limit < 0 or args.repeat < 1 or args.interval < 0 or args.typing_delay_ms < 0 or args.task_timeout <= 0:
        parser.error("执行数量、间隔和打字延迟不能为负数；repeat/timeout 必须大于0")
    try:
        return run(args)
    except KeyboardInterrupt:
        print("已中断，不自动重发题目。", file=sys.stderr)
        return 130
    except LockTimeout:
        print("已有脚本占用该浏览器配置或输出目录，请结束原进程。", file=sys.stderr)
        return 2
    except (StopBatch, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"网页自动操作停止：{type(exc).__name__}。请检查浏览器安装、页面结构和当前页面。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
