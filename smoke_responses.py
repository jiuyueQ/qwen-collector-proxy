#!/usr/bin/env python3
"""Manual live smoke check: streaming call -> simulated result -> linked answer.

This explicitly calls the model twice. No real business tools are executed.
"""
import argparse
import json
import os
from pathlib import Path

import httpx

from collector_proxy import ResponsesSSECollector


def generate(client, body):
    parser = ResponsesSSECollector()
    with client.stream("POST", "/v1/responses", json=body) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            parser.feed(chunk)
    parser.feed(b"", final=True)
    result = parser.result()
    if parser.errors or not parser.terminal or result.get("status") != "completed":
        raise RuntimeError("Responses 没有正常完成：" + json.dumps({
            "status": result.get("status"), "parse_errors": parser.errors,
            "error": result.get("error"), "incomplete_details": result.get("incomplete_details"),
        }, ensure_ascii=False))
    return result


def main():
    args_parser = argparse.ArgumentParser(description=__doc__)
    args_parser.add_argument("--base-url", default="http://127.0.0.1:9000")
    args_parser.add_argument("--model", default="Qwen/Qwen3.8-27B")
    args = args_parser.parse_args()
    base = args.base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    request = json.loads(Path(__file__).with_name("example_responses.json").read_text(encoding="utf-8"))
    request["model"] = args.model
    with httpx.Client(base_url=base, timeout=600,
            headers={"authorization": "Bearer " + os.getenv("PROXY_API_KEY", "EMPTY")}) as client:
        first = generate(client, request)
        calls = [item for item in first["output"] if item.get("type") == "function_call"]
        if not calls:
            raise RuntimeError("第一轮没有 function_call；检查上游 Responses 和工具解析器配置")
        results = []
        for item in calls:
            parameters = json.loads(item["arguments"])
            if item["name"] != "get_order_status" or parameters.get("order_id") != "A123":
                raise RuntimeError("第一轮工具或参数不符合示例预期")
            results.append({"type": "function_call_output", "call_id": item["call_id"],
                "output": json.dumps({"order_id": "A123", "status": "已发货", "simulated": True}, ensure_ascii=False)})
        second = generate(client, {"model": args.model, "previous_response_id": first["id"],
            "input": results, "instructions": "根据工具返回结果简短回答，不再调用工具。",
            "tools": request["tools"], "tool_choice": "none", "store": True,
            "stream": True, "max_output_tokens": 4096})
    print("第一轮 response_id:", first["id"])
    print("第二轮 response_id:", second["id"])
    for item in second["output"]:
        if item.get("type") == "message":
            print("最终回复:", "".join(part.get("text", "") for part in item.get("content", [])))
    print("请在 data/datasets/sft-*.jsonl 检查第二轮的 prompt 中包含原问题、工具调用及工具结果。")


if __name__ == "__main__":
    main()
