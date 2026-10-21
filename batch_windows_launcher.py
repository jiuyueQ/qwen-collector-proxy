#!/usr/bin/env python3
"""Visible browser automation launcher. Login is completed manually on the webpage."""
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
    print("脚本会打开浏览器，逐字输入问题、点击发送、等待页面回复。请在浏览器内登录。")
    print("默认：单并发、每组新会话、每组间隔3秒、最多3轮、单轮问题不超过4000字符。")
    print("保留平台的会话内记忆设置；平台需按 session/thread 隔离。生成令牌数量保持不高于4096。")
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
    default_output = f"data/browser/windows-{bank_id}-{datetime.now():%Y%m%d}"
    output = prompt("输出目录；同目录继续运行会跳过已有记录", default_output)
    print("\n浏览器：1. 本机 Microsoft Edge；2. 本机 Google Chrome；3. 已安装的 Playwright Chromium")
    while True:
        browser_choice = integer("浏览器编号", 1, 1)
        if browser_choice in {1, 2, 3}:
            break
        print("请选择1、2或3。")
    browser_channel = {1: "msedge", 2: "chrome", 3: "chromium"}[browser_choice]
    local_path.write_text(json.dumps({"page_url": page_url}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    command = [sys.executable, "-u", str(root / "batch_browser_questions.py"), "--page-url", page_url,
               "--input", str(questions), "--output", output, "--limit", str(limit),
               "--repeat", str(repeat), "--browser", browser_channel]
    print("\n准备打开浏览器。登录并进入运行页后，回到控制台按回车开始。Ctrl+C 可中止。", flush=True)
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
