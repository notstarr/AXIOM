#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def request(api_url: str, path: str, *, method: str = "GET", payload: Any = None) -> Any:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        api_url.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def worker_body(worker: dict[str, Any], *, disabled: bool | None = None) -> dict[str, Any]:
    return {
        "worker_type": worker["worker_type"],
        "capabilities": worker["capabilities"],
        "max_running": worker["max_running"],
        "priority": worker["priority"],
        "config": worker["config"],
        "disabled": worker["disabled"] if disabled is None else disabled,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Drain AXIOM by disabling new Worker claims")
    parser.add_argument("action", choices=["disable", "restore"])
    parser.add_argument("--api-url", default="http://127.0.0.1:8888")
    parser.add_argument("--state-file", type=Path, required=True)
    args = parser.parse_args()

    if args.action == "disable":
        workers = request(args.api_url, "/api/v1/workers")
        args.state_file.parent.mkdir(parents=True, exist_ok=True)
        args.state_file.write_text(
            json.dumps(workers, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(args.state_file, 0o600)
        for worker in workers:
            name = urllib.parse.quote(str(worker["name"]), safe="")
            request(
                args.api_url,
                f"/api/v1/workers/{name}",
                method="PUT",
                payload=worker_body(worker, disabled=True),
            )
        print(json.dumps({"disabled": [item["name"] for item in workers]}, ensure_ascii=False))
        return

    workers = json.loads(args.state_file.read_text(encoding="utf-8"))
    for worker in workers:
        name = urllib.parse.quote(str(worker["name"]), safe="")
        request(
            args.api_url,
            f"/api/v1/workers/{name}",
            method="PUT",
            payload=worker_body(worker),
        )
    print(json.dumps({"restored": [item["name"] for item in workers]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
