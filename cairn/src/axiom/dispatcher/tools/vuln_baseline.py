from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _fetch_vulns(server: str, project_id: str, timeout: float) -> list[dict]:
    base = server.rstrip("/")
    url = f"{base}/projects/{project_id}/vulns"
    req = urllib.request.Request(url=url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("unexpected API response: expected list")
    rows: list[dict] = []
    for item in payload:
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _print_summary(vulns: list[dict]) -> None:
    print(f"total={len(vulns)}")
    severity: dict[str, int] = {}
    for row in vulns:
        key = str(row.get("severity") or "unknown")
        severity[key] = severity.get(key, 0) + 1
    print("severity=" + json.dumps(severity, ensure_ascii=False, sort_keys=True))
    for row in vulns:
        print(
            "\t".join(
                [
                    str(row.get("id") or ""),
                    str(row.get("severity") or ""),
                    str(row.get("status") or ""),
                    str(row.get("status_reason") or ""),
                    str(row.get("title") or ""),
                ]
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print vulnerability baseline for one AXIOM project."
    )
    parser.add_argument("--server", required=True, help="AXIOM server base URL")
    parser.add_argument("--project", required=True, help="AXIOM project id")
    parser.add_argument(
        "--timeout", type=float, default=10.0, help="HTTP timeout seconds (default: 10)"
    )
    args = parser.parse_args()

    try:
        vulns = _fetch_vulns(args.server, args.project, args.timeout)
    except urllib.error.HTTPError as exc:
        print(f"error=http_{exc.code}", file=sys.stderr)
        return 2
    except urllib.error.URLError as exc:
        print(f"error=network {exc.reason}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive fallback
        print(f"error={exc}", file=sys.stderr)
        return 2

    _print_summary(vulns)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
