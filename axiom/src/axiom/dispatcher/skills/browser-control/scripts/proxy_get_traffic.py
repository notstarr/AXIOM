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
from pathlib import Path

DEFAULT_FILE = "/tmp/proxy_flows.jsonl"
DEFAULT_BACKUPS = 3
MAX_QUERY_RESULTS = 5000
STATUS_COUNT_LIMIT = 1000
READ_BLOCK_SIZE = 64 * 1024

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


def _backup_count() -> int:
    try:
        return min(max(int(os.environ.get("PROXY_FLOW_BACKUPS", DEFAULT_BACKUPS)), 0), 20)
    except (TypeError, ValueError):
        return DEFAULT_BACKUPS


def _flow_files(file_path: str) -> list[Path]:
    path = Path(file_path)
    return [path, *(Path(f"{path}.{index}") for index in range(1, _backup_count() + 1))]


def _iter_lines_reverse(path: Path):
    """Yield UTF-8 JSONL records newest-first without loading the file."""

    if not path.is_file():
        return
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        remainder = b""
        while position > 0:
            size = min(READ_BLOCK_SIZE, position)
            position -= size
            handle.seek(position)
            remainder = handle.read(size) + remainder
            parts = remainder.split(b"\n")
            remainder = parts[0]
            for line in reversed(parts[1:]):
                if line:
                    yield line.decode("utf-8", errors="replace")
        if remainder:
            yield remainder.decode("utf-8", errors="replace")


def load_flows(file_path: str, filter_kw: str, type_filter: str, last: int):
    limit = min(max(int(last or 1), 1), MAX_QUERY_RESULTS)
    flows = []
    for path in _flow_files(file_path):
        for line in _iter_lines_reverse(path):
            try:
                flow = json.loads(line)
            except (TypeError, ValueError):
                continue
            if filter_kw and filter_kw not in flow.get("url", ""):
                continue
            kind = classify(flow.get("content_type", ""))
            if type_filter != "all" and kind != type_filter:
                continue
            flow["_type"] = kind
            flows.append(flow)
            if len(flows) >= limit:
                return list(reversed(flows))
    return list(reversed(flows))


def cmd_clear(file_path: str):
    removed = 0
    for path in _flow_files(file_path):
        if path.exists():
            path.unlink()
            removed += 1
    if removed:
        print(f"✅ 已清空: {file_path}（{removed} 个日志分片）")
    else:
        print(f"(文件不存在: {file_path})")


def cmd_status(file_path: str):
    p = Path(file_path)
    existing = [path for path in _flow_files(file_path) if path.exists()]
    if not existing:
        print(f"流量文件不存在: {file_path}")
        return
    print(f"流量文件: {file_path}")
    print(f"当前大小: {p.stat().st_size if p.exists() else 0} bytes")
    print(f"含轮转分片: {len(existing)} 个，共 {sum(path.stat().st_size for path in existing)} bytes")
    recent_count = 0
    exhausted = True
    for path in existing:
        for _line in _iter_lines_reverse(path):
            recent_count += 1
            if recent_count >= STATUS_COUNT_LIMIT:
                exhausted = False
                break
        if not exhausted:
            break
    qualifier = "" if exhausted else "至少 "
    print(f"已捕获: {qualifier}{recent_count} 条（快速统计，不全量扫描大文件）")
    # 检查 mitmdump 是否在运行
    import subprocess
    try:
        result = subprocess.run(
            ["pgrep", "-f", "mitmdump"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
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
        print("\n--- 请求 Headers (安全相关) ---")
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
