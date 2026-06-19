#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
proxy_get_traffic.py — AI 调用此脚本查询 mitmproxy 捕获的流量

用法：
    python3 proxy_get_traffic.py [OPTIONS]

选项：
    --file FILE         JSONL 文件路径（默认 /tmp/proxy_flows.jsonl）
    --filter KEYWORD    只看 URL 包含此字符串的请求（如 /api/）
    --type TYPE         类型过滤：api（JSON接口）| page（HTML）| all（默认）
    --last N            只看最后 N 条（默认 30）
    --summary           单行摘要（默认），加 --full 显示完整响应体
    --full              显示完整请求/响应体
    --clear             清空日志文件
    --status            查看进程和文件状态

示例（AI 调用）：
    # 操作前清空
    python3 .../proxy_get_traffic.py --clear

    # 操作后查看触发的 API
    python3 .../proxy_get_traffic.py --filter /api/ --type api --summary

    # 查看某个页面跳转触发了什么
    python3 .../proxy_get_traffic.py --last 20 --summary

    # 查看某条请求的完整响应
    python3 .../proxy_get_traffic.py --filter /api/v1/user --full --last 1
"""

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_FILE = "/tmp/proxy_flows.jsonl"

API_CONTENT_TYPES = (
    "application/json", "application/xml", "text/xml",
    "application/graphql", "text/plain",
)


def classify(content_type: str) -> str:
    ct = content_type.lower()
    if any(t in ct for t in API_CONTENT_TYPES):
        return "api"
    if "text/html" in ct:
        return "page"
    return "other"


def load_flows(file_path: str, filter_kw: str, type_filter: str, last: int):
    p = Path(file_path)
    if not p.exists():
        return []

    flows = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                flow = json.loads(line)
            except Exception:
                continue

            if filter_kw and filter_kw not in flow.get("url", ""):
                continue

            ct = flow.get("content_type", "")
            kind = classify(ct)
            flow["_type"] = kind

            if type_filter != "all" and kind != type_filter:
                continue

            flows.append(flow)

    return flows[-last:] if last else flows


def cmd_clear(file_path: str):
    p = Path(file_path)
    if p.exists():
        p.unlink()
        print(f"✅ 已清空: {file_path}")
    else:
        print(f"(文件不存在: {file_path})")


def cmd_status(file_path: str):
    p = Path(file_path)
    if not p.exists():
        print(f"流量文件不存在: {file_path}")
        return
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    print(f"流量文件: {file_path}")
    print(f"已捕获: {len(lines)} 条")
    # 检查 mitmdump 是否在运行
    import subprocess
    result = subprocess.run(["pgrep", "-f", "mitmdump"], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"mitmdump 运行中 (pid={result.stdout.strip()})")
    else:
        print("mitmdump 未运行")


def print_summary(flows):
    if not flows:
        print("(无匹配记录)")
        return
    for f in flows:
        kind = f.get("_type", "?")
        status = f.get("status_code", "?")
        method = f.get("method", "?")
        url = f.get("url", "")
        req_body = f.get("request_body", "")
        resp_preview = f.get("response_body", "")[:120]
        print(f"[{kind}][{status}] {method} {url}")
        if req_body:
            print(f"  req: {req_body[:150]}")
        if resp_preview and kind == "api":
            print(f"  rsp: {resp_preview}")


def print_full(flows):
    if not flows:
        print("(无匹配记录)")
        return
    for f in flows:
        print(f"\n{'='*60}")
        print(f"[{f.get('_type','?')}][{f.get('status_code')}] {f.get('method')} {f.get('url')}")
        print(f"\n--- 请求 Headers (安全相关) ---")
        sec_keys = {'cookie','authorization','x-csrf-token','x-token','content-type','x-auth-token'}
        for k, v in f.get("request_headers", {}).items():
            if k.lower() in sec_keys:
                print(f"  {k}: {v[:200]}")
        if f.get("request_body"):
            print(f"\n--- 请求 Body ---\n{f['request_body'][:2000]}")
        print(f"\n--- 响应 Body ({len(f.get('response_body',''))} chars) ---")
        print(f.get("response_body", "")[:5000])


def main():
    parser = argparse.ArgumentParser(description="查询 mitmproxy 捕获的流量")
    parser.add_argument("--file", default=DEFAULT_FILE)
    parser.add_argument("--filter", "-f", default="", dest="filter_kw")
    parser.add_argument("--type", "-t", default="all",
                        choices=["api", "page", "other", "all"])
    parser.add_argument("--last", "-n", type=int, default=30)
    parser.add_argument("--summary", "-s", action="store_true", default=True)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.clear:
        cmd_clear(args.file)
        return

    if args.status:
        cmd_status(args.file)
        return

    flows = load_flows(args.file, args.filter_kw, args.type, args.last)

    if args.full:
        print_full(flows)
    else:
        print_summary(flows)


if __name__ == "__main__":
    main()
