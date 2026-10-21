#!/usr/bin/env python3
"""Interactive Windows entry point. Does not store passwords or login tokens."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys


def prompt(label: str, default: str = "") -> str:
    answer = input(label + (f" [{default}]" if default else "") + ": ").strip()
    return answer or default


def integer(label: str, default: int, minimum: int) -> int:
    while True:
        value = prompt(label, str(default))
        try:
            parsed = int(value)
            if parsed >= minimum:
                return parsed
        except ValueError:
            pass
        print(f"请输入不小于 {minimum} 的整数。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-url", help="可选，指定数字员工运行页地址")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    os.chdir(root)
    local_path = root / "windows_batch.local.json"
    local = json.loads(local_path.read_text(encoding="utf-8-sig")) if local_path.exists() else {}
    catalog = json.loads((root / "question_banks" / "catalog.json").read_text(encoding="utf-8"))
    print("\nWing 数字员工批量提问 — Windows\n")
    print("脚本会调用平台的实际 Agent 接口，账号密码与平台登录页相同。")
    print("默认：单并发、每组新会话、每组间隔3秒、最多3轮、单轮问题不超过4000字符。")
    print("可选择保留会话内记忆；平台需按 session/thread 隔离。生成令牌数量保持不高于4096。")
    page_url = args.page_url or prompt("运行页地址", local.get("page_url", ""))
    if not page_url:
        print("缺少页面地址。")
        return 2
    print("\n选择题库：")
    for index, bank in enumerate(catalog["banks"], 1):
        print(f"  {index}. {bank['title']}（{bank['count']} 组）")
    custom_index = len(catalog["banks"]) + 1
    print(f"  {custom_index}. 自己的 JSONL 题库")
    while True:
        choice = integer("题库编号，默认先跑只读查询", 1, 1)
        if choice <= custom_index:
            break
        print("没有这个题库编号。")
    if choice == custom_index:
        questions = Path(prompt("JSONL 文件完整路径").strip('"')).resolve()
        bank_id = "custom"
    else:
        bank = catalog["banks"][choice - 1]
        questions = root / "question_banks" / bank["file"]
        bank_id = bank["id"]
        print(bank["description"])
        if bank.get("contains_writes"):
            print("该题库含真实提交/保存指令，应对接业务测试环境，工具会按原配置执行。")
    if not questions.is_file():
        print("找不到题库文件：", questions)
        return 2
    limit = integer("本次最多执行多少组，0 为全部", 0, 0)
    repeat = integer("每组采样次数", 1, 1)
    default_output = f"data/batch/windows-{bank_id}-{datetime.now():%Y%m%d}"
    output = prompt("输出目录；同目录继续运行会跳过已有记录", default_output)
    print("\n记忆设置：1. 关闭平台记忆，使用严格检查；2. 保留平台的会话内记忆")
    print("选2时平台必须按 session/thread 分区保存记忆，不使用用户级跨会话共享记忆。")
    while True:
        memory_mode = integer("记忆模式", 2, 1)
        if memory_mode in {1, 2}:
            break
        print("请选择1或2。")
    tenant_code = prompt("平台租户编码", local.get("tenant_code", ""))
    username = prompt("平台用户名", local.get("username", ""))
    if not tenant_code or not username:
        print("租户编码和用户名不能为空。")
        return 2
    # Local convenience settings contain no password/token and are ignored by Git.
    local_path.write_text(json.dumps({"page_url": page_url, "tenant_code": tenant_code,
                                     "username": username}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    command = [sys.executable, "-u", str(root / "batch_agent_questions.py"), "--page-url", page_url,
               "--input", str(questions), "--output", output, "--limit", str(limit),
               "--repeat", str(repeat), "--login", "--tenant-code", tenant_code, "--username", username]
    if memory_mode == 2:
        command.append("--allow-agent-memory")
    print("\n开始运行。接下来输入平台密码，输入时不显示。Ctrl+C 可中止。", flush=True)
    try:
        return subprocess.call(command, cwd=root)
    except KeyboardInterrupt:
        print("\n已请求中止；请保留数据目录用于查看断点。")
        return 130


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"启动失败：{type(exc).__name__}。请检查本地配置和文件路径。", file=sys.stderr)
        raise SystemExit(2)
