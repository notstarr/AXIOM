from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request


def _fetch_assets(server: str, group_id: str, limit: int, offset: int) -> list[dict]:
    params = urllib.parse.urlencode(
        {"group_id": group_id, "limit": str(limit), "offset": str(offset)}
    )
    url = f"{server.rstrip('/')}/assets?{params}"
    req = urllib.request.Request(url=url, method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("unexpected API response for /assets")
    return [item for item in payload if isinstance(item, dict)]


def _format_asset(item: dict) -> str:
    method = str(item.get("api_method") or "")
    domain = str(item.get("backend_domain") or "")
    path = str(item.get("api_path") or "")
    status = str(item.get("test_status") or "unknown")
    return f"{method} {domain}{path} [{status}]"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print AXIOM asset baseline for one group."
    )
    parser.add_argument("--server", required=True, help="AXIOM server base URL")
    parser.add_argument("--group", required=True, help="AXIOM group id")
    parser.add_argument("--limit", type=int, default=15, help="page size")
    parser.add_argument("--offset", type=int, default=0, help="page offset")
    args = parser.parse_args()

    try:
        assets = _fetch_assets(
            server=args.server,
            group_id=args.group,
            limit=args.limit,
            offset=args.offset,
        )
    except Exception as exc:
        print(f"error={exc}", file=sys.stderr)
        return 2

    for item in assets:
        print(_format_asset(item))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
