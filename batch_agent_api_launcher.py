#!/usr/bin/env python3
"""Windows launcher for sequential Wing Agent API sessions."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys


def prompt(label, default=""):
    return input(label + (f" [{default}]" if default else "") + ": ").strip() or default


def number(label, default, minimum):
    while True:
        try:
            result = int(prompt(label, str(default)))
            if result >= minimum:
                return result
        except ValueError:
            pass
        print(f"请输入不小于{minimum}的整数。")


def main():
    root = Path(__file__).resolve().parent
    os.chdir(root)
    config_path = root / "agent_api.local.json"
    config = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.exists() else {}
    banks = json.loads((root / "question_banks/catalog.json").read_text(encoding="utf-8"))["banks"]
    print("\nWing Agent接口批量提问：一个会话结束后再开始下一个\n")
    print("一行题目对应一个独立会话；turns中的多轮使用同一会话。")
    print("单并发，组间隔3秒，轮间隔1秒。保留平台会话内记忆，不携带上一组历史。")
    page_url = prompt("数字员工运行页地址", config.get("page_url", ""))
    if not page_url:
        raise ValueError("缺少页面地址")
    for index, bank in enumerate(banks, 1):
        print(f"{index}. {bank['title']}（{bank['count']}组）")
    custom = len(banks) + 1
    print(f"{custom}. 自己的JSONL题目文件")
    while True:
        choice = number("选择题库", 1, 1)
        if choice <= custom:
            break
        print("没有这个题库。")
    if choice == custom:
        source = Path(prompt("题目文件路径").strip('"')).resolve()
        bank_id = "custom"
    else:
        bank = banks[choice - 1]
        source = root / "question_banks" / bank["file"]
        bank_id = bank["id"]
        print(bank["description"])
        if bank.get("contains_writes"):
            print("包含保存/提交操作，请使用业务测试环境。")
    if not source.is_file():
        raise ValueError("找不到题目文件")
    limit = number("本次最多执行多少个新会话，0为全部", 0, 0)
    repeat = number("每组重复采样次数，每次使用新会话", 1, 1)
    output = prompt("保存目录，同目录续跑跳过已有会话", f"data/agent-api/{bank_id}-{datetime.now():%Y%m%d}")
    tenant_code = prompt("平台租户编码", config.get("tenant_code", ""))
    username = prompt("平台用户名", config.get("username", ""))
    if not tenant_code or not username:
        raise ValueError("需要平台租户编码和用户名")
    config_path.write_text(json.dumps({"page_url": page_url, "tenant_code": tenant_code,
                                      "username": username}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    command = [sys.executable, "-u", str(root / "batch_agent_questions.py"), "--page-url", page_url,
               "--input", str(source), "--output", output, "--limit", str(limit), "--repeat", str(repeat),
               "--interval", "3", "--turn-interval", "1", "--login", "--tenant-code", tenant_code,
               "--username", username, "--allow-agent-memory"]
    print("\n开始登录；接下来输入平台密码，密码不显示、不保存。Ctrl+C中止。", flush=True)
    return subprocess.call(command, cwd=root)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("已中止，不自动重发。", file=sys.stderr)
        raise SystemExit(130)
    except (ValueError, OSError) as exc:
        print(f"无法启动：{exc}", file=sys.stderr)
        raise SystemExit(2)
