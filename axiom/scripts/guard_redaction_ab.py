#!/usr/bin/env python3
from __future__ import annotations

"""Replay recorded judge decisions with and without masking.

The claim behind ``guard.redact`` is "masking does not change the verdict".  A
unit test can only prove the regexes do what they say; this script is the
evidence for the claim itself.  It takes the Explore commands that were actually
judged, sends each one to the same judge twice - once verbatim, once masked - and
reports where the digit flipped.

Run it on the VPS, where the API key and the audit table live:

    .venv/bin/python scripts/guard_redaction_ab.py --task-id t_231 --limit 60
    .venv/bin/python scripts/guard_redaction_ab.py --task-id t_231 --dry-run

Exit code is 1 if any command gets a different verdict, so this can gate a
deploy.  A mismatch is not automatically a bug - read it: the two known benign
causes are judge jitter at temperature 0 (rare, but the model is not
deterministic across calls) and the genuine semantic loss from hiding a
credential, e.g. ``curl -u admin:admin`` no longer reads as a default-credential
probe.
"""

import argparse
import json
import sys
import time

import httpx
from sqlalchemy import text

from axiom.modules.guard.policy import _judge_input, _parse_verdict, _resolve_profile
from axiom.modules.guard.schemas import GuardRequest
from axiom.modules.settings.service import RuntimeSettingsService
from axiom.shared.database import create_database_engine


def _load_rows(engine, task_id: str, limit: int) -> list[dict]:
    query = text(
        """
        SELECT id, payload_json
        FROM axiom.audit_events
        WHERE event_type = 'guard.decision'
          AND task_id = :task_id
        ORDER BY id DESC
        LIMIT :limit
        """
    )
    rows: list[dict] = []
    with engine.connect() as conn:
        for row in conn.execute(query, {"task_id": task_id, "limit": max(limit * 6, 200)}):
            raw = row.payload_json
            try:
                payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except json.JSONDecodeError:
                continue
            if payload.get("source") != "model":
                continue
            command = str(payload.get("command") or "").strip()
            if not command:
                continue
            rows.append(
                {
                    "event_id": row.id,
                    "tool_name": payload.get("tool_name") or "bash",
                    "command": command,
                    "phase": payload.get("phase") or "",
                    "engagement_mode": payload.get("engagement_mode") or "",
                    "recorded_decision": payload.get("decision") or "",
                    "recorded_reason": payload.get("reason") or "",
                }
            )
            if len(rows) >= limit:
                break
    return rows


def _ask(config, prompt: str, content: str) -> tuple[str, str, int]:
    """One judge call.  Returns ``(verdict, reason, latency_ms)``."""

    started = time.perf_counter()
    payload = {
        "model": config.guard_model_name.strip(),
        "messages": [
            {"role": "system", "content": prompt.strip()},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_tokens": 64,
    }
    if config.guard_thinking_enabled is False:
        payload["enable_thinking"] = False
    proxy = (
        config.network_proxy_url.strip()
        if config.network_proxy_mode == "http" and config.network_proxy_url.strip()
        else None
    )
    with httpx.Client(timeout=config.guard_timeout_seconds, proxy=proxy) as client:
        response = client.post(
            f"{config.guard_model_base_url.strip().rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {config.guard_api_key.strip()}"},
            json=payload,
        )
    response.raise_for_status()
    raw = str(response.json()["choices"][0]["message"]["content"])
    verdict, reason = _parse_verdict(raw)
    return verdict, reason, int((time.perf_counter() - started) * 1000)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True, help="task whose recorded judge calls to replay")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--dry-run", action="store_true", help="print the masked text, call no model")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    engine = create_database_engine(application_name="axiom-guard-ab")
    config = RuntimeSettingsService(engine).get().config
    rows = _load_rows(engine, args.task_id, args.limit)
    if not rows:
        print(f"没有找到可复现的 model 判定记录（task={args.task_id}）", file=sys.stderr)
        return 2

    results: list[dict] = []
    for row in rows:
        request = GuardRequest(
            tool_name=row["tool_name"],
            input={"command": row["command"]},
            phase=row["phase"],
        )
        normalized = {"tool_name": row["tool_name"], "command": row["command"]}
        masked, redacted_count = _judge_input(normalized, request)
        raw_text = f"{row['tool_name']}  {row['command']}"
        if row["phase"]:
            raw_text += f"\n[phase] {row['phase']}"

        entry = {**row, "masked": masked, "redacted_count": redacted_count}
        if args.dry_run:
            results.append(entry)
            continue

        prompt, _rules = _resolve_profile(config, row["engagement_mode"])
        raw_verdict, raw_reason, raw_ms = _ask(config, prompt, raw_text)
        masked_verdict, masked_reason, masked_ms = _ask(config, prompt, masked)
        entry.update(
            {
                "raw_verdict": raw_verdict,
                "raw_reason": raw_reason,
                "raw_ms": raw_ms,
                "masked_verdict": masked_verdict,
                "masked_reason": masked_reason,
                "masked_ms": masked_ms,
                "same": raw_verdict == masked_verdict and raw_verdict in {"0", "1"},
            }
        )
        results.append(entry)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for item in results:
            if args.dry_run:
                marker = "*" if item["redacted_count"] else " "
                print(f"{marker} {item['redacted_count']:>2}  {item['masked'][:150]}")
                continue
            marker = "  一致" if item["same"] else "!!差异"
            print(f"{marker}  raw={item['raw_verdict'] or '?'} masked={item['masked_verdict'] or '?'}")
            print(f"      原文: {item['command'][:160]}")
            if item["redacted_count"]:
                print(f"      送审: {item['masked'][:160]}")
            if not item["same"]:
                print(f"      raw 原因: {item['raw_reason']}")
                print(f"      masked 原因: {item['masked_reason']}")
            print(
                f"      延迟 raw={item['raw_ms']}ms masked={item['masked_ms']}ms"
            )

    if args.dry_run:
        replaced = sum(1 for item in results if item["redacted_count"])
        print(f"\n{replaced}/{len(results)} 条会被脱敏", file=sys.stderr)
        return 0

    compared = [item for item in results if "same" in item]
    agree = sum(1 for item in compared if item["same"])
    rate = (agree / len(compared) * 100) if compared else 0.0
    raw_p50 = sorted(item["raw_ms"] for item in compared)[len(compared) // 2] if compared else 0
    masked_p50 = sorted(item["masked_ms"] for item in compared)[len(compared) // 2] if compared else 0
    print(
        f"\n一致 {agree}/{len(compared)}（{rate:.1f}%），"
        f"P50 延迟 raw={raw_p50}ms masked={masked_p50}ms",
        file=sys.stderr,
    )
    return 0 if agree == len(compared) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
