#!/usr/bin/env python3
"""Read miniapp request templates from AXIOM credential store.

This helper prints credential metadata and a redacted request template.  It does
not send business requests.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.parse import urlparse

import requests

SECRET_KEYS = {"token", "x-token", "cookie", "authorization", "session", "password", "secret", "openid", "unionid", "phone", "mobile"}


def redact_value(key: str, value: Any) -> Any:
    if isinstance(value, dict):
        return {k: redact_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(key, item) for item in value]
    if any(secret in key.lower() for secret in SECRET_KEYS):
        text = str(value)
        if not text:
            return value
        return text[:4] + "..." + text[-4:] if len(text) > 10 else "***"
    return value


def load_credentials(server: str, project_id: str | None, cred_id: str | None) -> list[dict]:
    token = os.getenv("AXIOM_SERVICE_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if cred_id:
        url = server.rstrip("/") + "/api/v1/internal/credentials/" + cred_id
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return [resp.json()]
    if not project_id:
        raise SystemExit("--project-id is required when --cred-id is not provided")
    url = server.rstrip("/") + f"/api/v1/projects/{project_id}/credentials"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    summaries = resp.json()
    credentials = []
    for summary in summaries:
        secret = requests.get(
            server.rstrip("/") + "/api/v1/internal/credentials/" + summary["id"],
            headers=headers,
            timeout=10,
        )
        secret.raise_for_status()
        credentials.append(secret.json())
    return credentials


def parse_json_field(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return raw


def domain_matches(credential: dict, profile: dict) -> bool:
    profile_hosts = {str(host).lower() for host in profile.get("hosts", [])}
    if not profile_hosts:
        return True
    domains = parse_json_field(credential.get("domains") or [])
    if isinstance(domains, str):
        domains = [domains]
    for item in domains or []:
        host = str(item).lower()
        parsed = urlparse(host)
        if parsed.netloc:
            host = parsed.netloc.lower()
        if host in profile_hosts or any(host.endswith("." + target) for target in profile_hosts):
            return True
    return False


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Print redacted miniapp request templates from AXIOM credentials")
    parser.add_argument("--server", default="http://127.0.0.1:8888", help="AXIOM server URL")
    parser.add_argument("--project-id", default=os.getenv("AXIOM_PROJECT_ID", ""), help="V2 Project id")
    parser.add_argument("--cred-id", help="specific credential id")
    parser.add_argument("--profile", help="profile JSON path")
    parser.add_argument("--show-secret", action="store_true", help="print secrets without redaction; use only in private scratch files")
    args = parser.parse_args(argv)

    profile = {}
    if args.profile:
        with open(args.profile, "r", encoding="utf-8") as fh:
            profile = json.load(fh)
    credentials = load_credentials(args.server, args.project_id, args.cred_id)
    selected = []
    for cred in credentials:
        cj = parse_json_field(cred.get("data") or {})
        if isinstance(cj, dict) and cj.get("type") != "miniapp_request_template":
            continue
        if not domain_matches(cred, profile):
            continue
        item = dict(cred)
        item["data"] = cj
        selected.append(item)

    output = []
    for cred in selected:
        item = {
            "id": cred.get("id"),
            "name": cred.get("label"),
            "active": cred.get("active"),
            "auth_type": cred.get("auth_type"),
            "domains": parse_json_field(cred.get("domains") or []),
            "data": cred.get("data"),
        }
        if not args.show_secret:
            item = redact_value("", item)
        output.append(item)
    print(json.dumps({"count": len(output), "credentials": output}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
