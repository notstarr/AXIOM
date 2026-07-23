from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request


def _get_json(url: str) -> object:
    req = urllib.request.Request(url=url, method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _list_credentials(server: str, group_id: str) -> int:
    url = f"{server.rstrip('/')}/credentials?{urllib.parse.urlencode({'group_id': group_id})}"
    data = _get_json(url)
    if not isinstance(data, list):
        raise ValueError("unexpected credentials response")
    for item in data:
        if not isinstance(item, dict):
            continue
        cred_id = str(item.get("id") or "")
        label = str(item.get("label") or "")
        auth_type = str(item.get("auth_type") or "")
        asset_id = str(item.get("asset_id") or "")
        captured = str(item.get("captured_at") or "")
        cred_json = str(item.get("credentials_json") or "")
        print(
            f"[{cred_id}] label={label} type={auth_type} asset_id={asset_id} captured={captured}"
        )
        print(f"  credentials_json: {cred_json[:200]}")
    return 0


def _list_credential_labels(server: str, group_id: str) -> int:
    url = f"{server.rstrip('/')}/credentials?{urllib.parse.urlencode({'group_id': group_id})}"
    data = _get_json(url)
    if not isinstance(data, list):
        raise ValueError("unexpected credentials response")
    for item in data:
        if not isinstance(item, dict):
            continue
        print(str(item.get("id") or ""), str(item.get("label") or ""))
    return 0


def _show_asset_login(server: str, asset_id: str) -> int:
    url = f"{server.rstrip('/')}/assets/{asset_id}"
    data = _get_json(url)
    if not isinstance(data, dict):
        raise ValueError("unexpected asset response")
    cfg = json.loads(data.get("login_config_json") or "{}")
    print(f"URL: {data.get('backend_domain','')}{data.get('api_path','')}")
    print(f"Method: {cfg.get('method', 'POST')}")
    print(f"Content-Type: {cfg.get('content_type', 'application/json')}")
    print(f"Accounts: {json.dumps(cfg.get('accounts', {}), ensure_ascii=False)[:300]}")
    print(
        f"Body template: {json.dumps(cfg.get('body_template', {}), ensure_ascii=False)[:300]}"
    )
    print(f"Token path: {cfg.get('response_token_path', '')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect AXIOM credential/asset data.")
    parser.add_argument("--server", required=True, help="AXIOM server base URL")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("list-creds", help="list credentials with previews")
    p1.add_argument("--group", required=True, help="group id")

    p2 = sub.add_parser("list-labels", help="list credential id + label")
    p2.add_argument("--group", required=True, help="group id")

    p3 = sub.add_parser("show-asset-login", help="show login config by asset id")
    p3.add_argument("--asset-id", required=True, help="asset id")

    args = parser.parse_args()
    try:
        if args.cmd == "list-creds":
            return _list_credentials(args.server, args.group)
        if args.cmd == "list-labels":
            return _list_credential_labels(args.server, args.group)
        return _show_asset_login(args.server, args.asset_id)
    except Exception as exc:
        print(f"error={exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
