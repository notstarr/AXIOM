# Crawler Next-Agent Handoff

## Executive Summary (updated)

The dominant blocker described in the previous version of this doc — `browser-use` session/daemon instability against `Chrome-SecTest` on port `9223` — has been **resolved**. The browser-control layer was replaced with a stateless Chrome DevTools Protocol CLI (`cdp_ctl.py`) that has no daemon, no sessions, and no config-conflict class. The crawler now runs end-to-end: `crawl` reliably drives the browser, emits valid `{accepted:true}` incremental payloads, and they are merged into backend state, with rounds incrementing past 1.

This was validated live: a full crawl against a live target produced real structured records (pages with `discovered_via`/`actions`/`exhausted`, APIs with status codes, page↔API mappings) that landed in the `crawler_pages / crawler_apis / crawler_page_maps` tables across multiple successful rounds.

The only remaining live issue is **external**: the project's own target `*.redhaze.top` (47.86.177.216) suffered a degraded state (TCP up, TLS handshake hangs) during validation, so the real `pg_006` run could not complete against it at that moment. A background monitor relaunches `pg_006` automatically once the target recovers. The code is ready; this is purely target availability.

## What Was Changed This Round

### 1. New: stateless CDP helper — `tools/crawler/cdp_ctl.py`

Replaces `browser-use` entirely for the crawler. Key properties:

- **No daemon, no sessions, no persistent state.** Each invocation connects to Chrome on `--cdp` (default `http://localhost:9223`), performs ONE action, exits. The "Session already running with different config" failure class is eliminated by construction.
- **One persistent WebSocket per command** (`CDPSession`) that auto-dismisses JS dialogs (alert/confirm/beforeunload) and is resilient to recv timeouts, so pages can never block eval.
- **Dedicated-tab model**: `new-tab <url>` creates a tab and waits for load; `--tab-id <id>` (or `CDP_TAB_ID` env) targets it directly; `close-tab <id>` cleans up. The crawler owns one tab per run instead of reusing possibly-stuck tabs.
- Commands: `state` (URL/title/readyState + indexed interactive elements), `links`, `url`, `title`, `text`, `eval`, `click <idx>`, `click-text`, `click-href`, `open`, `screenshot`, `new-tab`, `close-tab`.
- Run with the project venv python (which has `websocket-client`): `python3 .../cdp_ctl.py ...`

### 2. `crawler.py` — dedicated-tab lifecycle + browser-use removal

- Added `_CDP_PY` / `_CDP_CTL` / `_CDP_ENDPOINT` constants and helpers `_cdp_ctl`, `_create_crawler_tab`, `_close_crawler_tab`, `_ensure_crawler_tab`.
- `_run_crawler_supervisor` now ensures a live dedicated CDP tab at start and before each crawl round, and closes it in a `finally` block on exit.
- `_run_phase_once` passes the CDP tab id as `{intent_id}` into the prompt (so the agent always uses `--tab-id <id>`), and **removed** the flush-phase `browser-use --session ... close` subprocess call (it was killing real browser pages and is obsolete).
- `start_crawler` closes any orphaned tab from a previous run before starting fresh, and initializes `cdp_tab_id` in meta.

### 3. Prompts rewritten to drop browser-use

- `CrawlerExecute.md` / `CrawlerFlush.md` now instruct the agent to use `cdp_ctl.py` with `--tab-id {intent_id}`, and explicitly forbid `browser-use`. All the `pkill`/`close`/retry stability noise was removed — it is no longer needed.

### 4. Tests

- `uv run pytest tests/test_crawler_runtime.py tests/test_server_api.py` → **18 passed**.
- The new tab-lifecycle code is exercised indirectly; the existing phase/payload/traffic-merge tests still cover the backend merge path. (One test initially broke because an over-eager "no tab → skip crawl" early-exit bypassed the worker; that early-exit was removed since the supervisor already guarantees a tab in production.)

## Live Validation (proof the pipeline works)

A validation crawl was run against a live target (`https://example.com`) using the real worker (`deepseek-v4-pro`) and the real Chrome on `9223`:

- Round 1: agent ran `cdp_ctl --tab-id ... state`, explored, emitted `{"accepted":true,"data":{...pages/apis/mappings...}}` → extracted → validated → merged into DB.
- Round 2: agent clicked "Learn more", discovered the IANA page, emitted a richer increment → merged. DB ended with 2 pages, 1 API, 1 mapping, all with real schema fields (`discovered_via`, `actions`, `exhausted`, `status_codes_seen`).
- Rounds incremented past 1 (the old "round 1 always terminates early" failure mode is gone). No `browser-use` conflicts occurred at all.

## Remaining Work

1. **Target availability (external, blocking only the real `pg_006` run).** `*.redhaze.top` was degraded (TCP up, TLS hangs) at validation time. When it recovers, start `pg_006` from the UI/API (or the background monitor will). No code change needed.
2. Once `pg_006` runs against the live redhaze target, confirm `crawl` emits valid payloads on the real site and `flush` only triggers on genuine fallbacks.
3. Optionally remove legacy YAML-ingest/export code further if still unused (low priority; state-backed views already work without physical files).

## Files To Read First (next agent)

- `tools/crawler/cdp_ctl.py` — the new browser-control helper (start here)
- `cairn/src/axiom/server/routers/crawler.py` — tab lifecycle (`_ensure_crawler_tab`, `_run_crawler_supervisor`, `_run_phase_once`)
- `cairn/src/axiom/dispatcher/prompts/other/CrawlerExecute.md` / `CrawlerFlush.md` — agent instructions
- `cairn/tests/test_crawler_runtime.py` — backend merge behavior

## One-Sentence Handoff

The crawler's browser-control instability is fixed by replacing `browser-use` with a stateless CDP CLI (`tools/crawler/cdp_ctl.py`) plus a dedicated-tab lifecycle in `crawler.py`; the full pipeline (drive → emit `{accepted:true}` payload → validate → merge to backend state → round++) is proven working end-to-end live, and the only thing left is for the project's own redhaze target to come back online so `pg_006` can run for real.

## Final Product Direction Agreed With User

The user clarified the intended model very explicitly. This is the canonical target:

### State Ownership

- backend state is the only authority
- YAML is a logical backend-maintained view
- physical YAML files do not need to be written on every round

### Phase Semantics

- `crawl` is the main execution path
- `crawl` explores and directly returns an incremental payload
- `flush` is **not** a normal "organize and write YAML" phase
- `flush` is only a fallback, similar to `explore_conclude`
- `flush` should run only when `crawl`:
  - fails
  - times out
  - returns invalid output

### Payload Contract

- `crawl` and `flush` should return the **same payload shape**
- backend parses and validates that one shape once
- backend merges it into the three crawler state domains:
  - site map
  - API inventory
  - page/API mapping

### Exploration Granularity

The user chose:

- `crawl` should stop after a small chunk of meaningful progress
- recommended budget: at most **2 meaningful actions**
- stop earlier once there is enough incremental value to commit

### What "timely persistence" means

The user's core requirement is:

> whatever the crawler has actually clicked and discovered must be reflected in maintained state quickly, instead of the browser continuing to run far ahead while YAML/state remains stale.

This does **not** necessarily mean every single click must be synchronously persisted after the click. It means the system must not allow large divergence between actual exploration progress and maintained state.

## What Has Already Been Changed

## 1. Backend Protocol Foundation

In `cairn/src/axiom/server/routers/crawler.py`, the following backend pieces were added or reworked:

- unified crawler increment validation
- extraction of phase output from session/logs
- backend merge into crawler state tables
- state-backed logical view rendering

Important new internal helpers include:

- `_validate_crawler_increment_payload(...)`
- `_extract_phase_increment_payload(...)`
- `_apply_crawler_increment(...)`
- `_render_crawler_state_snapshot(...)`
- `_crawler_state_view_exists(...)`

The protocol target is that both `crawl` and `flush` produce a payload like:

```json
{
  "accepted": true,
  "data": {
    "description": "...",
    "pages": [],
    "apis": [],
    "mappings": []
  }
}
```

## 2. State-Only View Access

In `cairn/src/axiom/server/routers/files.py`, group crawler file endpoints were extended so that:

- if physical files do not exist
- but crawler state exists

the endpoints can still return:

- `site_map.yaml`
- `api_inventory.yaml`
- `page_api_map.yaml`

as logical views rendered from backend state.

This is important because it decouples preview/state inspection from physical file materialization.

## 3. Prompt Direction Was Updated

Both crawler prompts were rewritten away from the old YAML-first framing:

- `cairn/src/axiom/dispatcher/prompts/other/CrawlerExecute.md`
- `cairn/src/axiom/dispatcher/prompts/other/CrawlerFlush.md`

Current intent:

- `CrawlerExecute` should return structured incremental payload
- `CrawlerFlush` is a fallback phase and should also return the same payload shape

## 4. Tests Added / Updated

The following tests now pass:

- `cairn/tests/test_crawler_runtime.py`
- `cairn/tests/test_server_api.py`

Verified command:

```bash
uv run pytest tests/test_crawler_runtime.py tests/test_server_api.py -q
```

Latest observed result:

- `18 passed, 1 warning`

## What Has Been Proven

## 1. Old "stale YAML rewrite" bug was real and has been corrected

Previously the system could:

- fail to produce fresh crawler output in a phase
- then still ingest/export old YAML-like state
- creating the illusion of progress while real discoveries were lost

That specific failure mode is no longer the main blocker.

## 2. State-backed logical crawler views work

The following endpoints can now serve logical crawler views even without requiring physical files:

- `/file-groups/{group_id}/crawler/site_map.yaml`
- `/file-groups/{group_id}/crawler/api_inventory.yaml`
- `/file-groups/{group_id}/crawler/page_api_map.yaml`

## 3. The "old meta polluting new run" theory was disproved

The runtime artifacts for `pg_006` were explicitly cleaned:

- `/tmp/axiom-crawler-pg_006.meta.json`
- `/tmp/axiom-crawler-pg_006.log`
- `/tmp/axiom-crawler/pg_006`
- `/tmp/proxy_flows.jsonl`

Then a clean rerun was started and observed.

The new run correctly showed a new:

- `supervisor_id`
- `started_at`
- `phase_pid`

So the remaining live failure is **not** just stale metadata from an old run.

## Current Live Blocker

The dominant live blocker is browser control instability.

### Symptoms seen repeatedly in `pg_006`

From recent live sessions:

- `browser-use --session crawler-pg_006 state` may work once, then fail
- `eval` may succeed once, then later fail with session-config conflict
- `screenshot` may succeed while `state` fails
- repeated daemon/session conflicts occur even after close/retry patterns

Typical error:

```text
Session 'crawler-pg_006' is already running with different config.
Run `browser-use --session crawler-pg_006 close` first.
```

Other observed errors:

- `Unauthorized`
- `Could not discover a running Chrome instance with remote debugging enabled`

### Important observation

Raw Chrome DevTools inspection still works:

- `curl http://localhost:9223/json/list`

This strongly suggests the instability is in `browser-use` session/daemon management, not in CDP availability itself.

## What The Live Session Shows Right Now

The latest `crawl` session under the new prompt **does** receive the updated instructions and begins acting on them:

- it reads current crawler state
- it inspects page structure
- it identifies meaningful actions like:
  - finance section
  - mall link
  - desk link
  - new ticket flow

But it then gets dragged into repeated tool-stability handling and cannot reliably finish the intended 1-2 meaningful action chunk and emit a valid incremental payload.

So:

- backend protocol is improved
- prompt contract is improved
- live execution still degrades because the browser tool layer is unstable

## Current Runtime Semantics vs Intended Semantics

### Intended

```text
crawl
  -> valid incremental payload
  -> backend merge
  -> next crawl round

if crawl fails / times out / invalid output:
  -> flush fallback
  -> valid incremental payload
  -> backend merge
```

### What still happens live

```text
crawl
  -> tool/session instability
  -> no valid payload
  -> needs_flush

flush
  -> fallback tries to recover increment
```

This is why `round 1` still often terminates early in live runs.

## Recommended Next Step

Do **not** spend more time trying to micro-tune prompt wording around `browser-use`.

The best next step is to replace crawler browser driving with a more stable CDP helper.

### Recommended direction

Build a small Python helper around Chrome DevTools Protocol for the crawler only.

The helper should support a minimal set of operations:

- get current page URL/title
- list interactive elements
- click by selector or href or DOM text
- execute JS
- capture screenshot
- inspect page state

Then update crawler prompts to use that helper instead of repeated `browser-use` daemon/session commands.

### Why this is the highest-value next step

- raw CDP endpoint is working
- `browser-use` daemon/session behavior is unstable in this environment
- the crawler only needs a constrained subset of browser actions
- a direct helper would remove the current session/config conflict class entirely

## Secondary Follow-Up Work

After replacing or stabilizing browser control:

1. verify `crawl` actually emits valid payloads in live runs
2. verify `flush` only runs on fallback paths
3. verify `round` increments beyond 1 on clean reruns
4. remove or downgrade legacy YAML-ingest/export code further, if still unnecessary

## Files To Read First

The next agent should start with:

- `cairn/src/axiom/server/routers/crawler.py`
- `cairn/src/axiom/server/routers/files.py`
- `cairn/src/axiom/dispatcher/prompts/other/CrawlerExecute.md`
- `cairn/src/axiom/dispatcher/prompts/other/CrawlerFlush.md`
- `cairn/tests/test_crawler_runtime.py`
- `cairn/tests/test_server_api.py`

For live evidence, inspect:

- `/tmp/axiom-crawler-pg_006.log`
- `/tmp/axiom-crawler/pg_006/sessions/*.jsonl`

## One-Sentence Handoff

The crawler backend has been moved significantly toward the intended model of `crawl` returning structured increments, `flush` acting only as fallback, and backend state being authoritative with YAML as a logical view; the main remaining blocker is live browser execution instability in `browser-use` session/daemon handling against `Chrome-SecTest` on port `9223`, which prevents `crawl` from reliably returning valid incremental payloads in real runs.
