#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two normalized AXIOM API snapshots")
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    left = json.loads(args.left.read_text(encoding="utf-8"))
    right = json.loads(args.right.read_text(encoding="utf-8"))
    keys = sorted(set(left) | set(right))
    results = []
    for key in keys:
        left_value = left.get(key, {"__missing__": True})
        right_value = right.get(key, {"__missing__": True})
        left_hash = hashlib.sha256(canonical(left_value)).hexdigest()
        right_hash = hashlib.sha256(canonical(right_value)).hexdigest()
        results.append(
            {
                "section": key,
                "left_sha256": left_hash,
                "right_sha256": right_hash,
                "matches": left_hash == right_hash,
            }
        )
    payload = {"matches": all(item["matches"] for item in results), "sections": results}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if not payload["matches"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
