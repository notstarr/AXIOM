# Crawler Handoff

This document summarizes the crawler work completed so far, the current architecture, the known gaps, and the recommended next steps for the next agent.

## Goal Of This Workstream

The crawler was being reworked from a single long-running prompt that directly edited YAML into a phased crawler flow closer to the main task loop style:

- `crawl`
- `flush`
- `final_flush`

And then further evolved toward a backend-owned crawler state model, where final YAML files are treated more like exported views than the only source of truth.

## High-Level Design Direction Chosen

The current direction is:

1. Use a phased crawler runner with hard phase switching.
2. Keep crawler-specific global timing settings in `/settings`.
3. Reuse the same browser session across phases.
4. Let flush persist findings and export YAML.
5. Start moving toward backend-owned crawler state tables:
   - `crawler_pages`
   - `crawler_apis`
   - `crawler_page_maps`

This is inspired by the main `bootstrap -> reason -> explore` loop, where agents return structured increments and the backend maintains the authoritative state.

## Files Touched In This Workstream

### Backend

- `cairn/src/axiom/server/db.py`
- `cairn/src/axiom/server/models.py`
- `cairn/src/axiom/server/routers/settings.py`
- `cairn/src/axiom/server/routers/crawler.py`
- `cairn/src/axiom/cli.py`

### Prompts

- `cairn/src/axiom/dispatcher/prompts/other/Crawler.md`
- `cairn/src/axiom/dispatcher/prompts/other/CrawlerExecute.md`
- `cairn/src/axiom/dispatcher/prompts/other/CrawlerFlush.md`

### Frontend

- `cairn/src/axiom/server/static/index.html`

### Docs

- `docs/crawler-output-schema.md`

## Important Commits In Order

These commits are the key milestones of the crawler refactor:

1. `76f8052 docs(crawler): define output schema v1`
2. `c452aeb feat(crawler): switch outputs to schema v1`
3. `e15fe22 fix(crawler): detect schema v1 output files`
4. `f4a5b92 feat(crawler): add global timing settings`
5. `bb4068f test(settings): fix crawler timing assertion`
6. `fba9523 feat(crawler): run phased execute and flush loops`
7. `f8838fc feat(crawler): persist phased state before export`
8. `387014d fix(crawler): persist state rows correctly`
9. `cf0a9b7 feat(crawler): add snapshot preview action`
10. `31eab01 feat(crawler): split snapshot preview actions`
11. `4ec57a7 style(crawler): stack snapshot actions vertically`
12. `ffabe5b style(preview): prefer vertical YAML scrolling`

## What Was Implemented

### 1. Schema v1 For Crawler Outputs

The intended crawler outputs were redesigned as three business-model-oriented files:

- `site_map.yaml`
- `api_inventory.yaml`
- `page_api_map.yaml`

The schema definition lives in:

- `docs/crawler-output-schema.md`

### 2. Global Crawler Timing Settings

Added to `/settings`:

- `crawler_active_window_seconds`
- `crawler_flush_window_seconds`
- `crawler_total_timeout_seconds`

Defaults currently:

- active: `180`
- flush: `45`
- total: `1800`

These are now stored in the `settings` table and surfaced in the settings UI.

### 3. Phase-Aware Crawler Status

`CrawlerStatus` was expanded to include:

- `status`
- `phase`
- `round`
- `pid`
- `supervisor_id`
- `phase_pid`
- `started_at`
- `phase_started_at`
- `last_flush_at`
- `active_window_seconds`
- `flush_window_seconds`
- `total_timeout_seconds`
- `stop_requested`
- file existence booleans

### 4. Phase-Specific Prompts

The crawler was split into:

- `CrawlerExecute.md`
- `CrawlerFlush.md`

Current intention:

- execute phase: explore and gather
- flush phase: write/merge/export, not broad exploration

### 5. Dedicated Crawler Supervisor CLI Entry

Added a dedicated CLI command:

- `axiom crawler-supervisor --project-id <group_id>`

The goal was to stop relying on in-server background threads and to run phased crawling as a separate runner process, closer to dispatcher-style control.

### 6. Crawler Snapshot Preview In Frontend

The crawler UI now has three separate snapshot preview actions:

- view site map
- view API inventory
- view page/API map

These reuse the existing YAML preview modal and read files from:

- `/file-groups/{group_id}/crawler/{filename}`

The preview area was also adjusted to prefer vertical scrolling over horizontal overflow.

## Current Architecture State

### What Works

1. Global crawler timings are configured and visible.
2. Frontend displays phase/round/flush timing info.
3. Snapshot preview for crawler outputs is available as three separate buttons.
4. New schema filenames are recognized by crawler status.
5. The phased crawler can run through multiple rounds (`crawl -> flush -> crawl`).
6. Flush can produce the three YAML files.
7. A backend crawler state model was added in DB:
   - `crawler_pages`
   - `crawler_apis`
   - `crawler_page_maps`
8. State ingestion bug for those tables was fixed.

### Important Verified Result

The final ingestion bug was fixed by explicitly mapping ID columns:

- `crawler_pages -> page_id`
- `crawler_apis -> api_id`
- `crawler_page_maps -> map_id`

Without that fix, state rows remained at zero.

## Current Verified Shortcomings

This is the most important part for the next agent.

### 1. State Store Is Not Yet The Sole Authority In Practice

Even though the state tables exist and manual verification showed rows can be inserted, the current phased flow is still heavily dependent on the agent writing schema-like YAML first.

The current flow is still:

1. crawler writes YAML-like schema files
2. backend ingests them into state tables
3. backend re-exports them

This is better than pure direct-file authority, but still not as robust as the main task loop architecture.

### 2. Flush Still Relies Too Much On Agent Obedience

Even after narrowing `CrawlerFlush.md`, session traces showed that flush sometimes still:

- uses `browser-use open ...`
- attempts browser exploration-like behavior
- runs into session conflicts

This means flush is still not reliably acting like a narrow conclude phase.

### 3. Phase Execution Became More Stable, But Not Fully Deterministic

At various points during the workstream:

- thread-based runner behavior was unstable
- server PID was mistakenly treated as crawler PID
- phase logs and round counters sometimes appeared inconsistent

The dedicated CLI runner improved things, but the next agent should still treat the phased runner as partially stabilized, not fully solved.

### 4. Output Quality Is Better But Still Sparse

When the crawler does produce schema content, the output is useful but still incomplete.

Typical problems observed:

- page coverage is partial
- API coverage is partial
- page->action->API mapping density is still low
- parameter provenance exists for some APIs but is not systematic yet

### 5. Completion Semantics Still Need Improvement

There is still no strong explicit `completion_reason` model such as:

- `timeout`
- `exhausted`
- `stop_requested`
- `failed`

Without this, `done` remains ambiguous.

## Why The Main Loop Is More Stable

The `bootstrap/reason/explore` loop was inspected during this workstream.

What was learned:

- agents in the main loop return small structured payloads
- backend updates authoritative graph/database state
- YAML is produced by export from DB state

This is the core stability advantage of the main loop.

The crawler still only partially follows that model.

## Recommended Next Steps For The Next Agent

### Priority 1: Move From File-Ingested State To Direct Incremental State Updates

The crawler should evolve from:

- agent writes full schema files
- backend ingests them

to:

- agent returns small structured crawler increments
- backend merges them directly into crawler state tables
- export derives the three YAML files from crawler state

This is the single most important architectural next step.

### Priority 2: Narrow Flush Further Toward Explore-Conclude Style

`CrawlerFlush.md` should continue to shrink in scope.

Desired behavior:

- no broad exploration
- no session wrestling
- no re-opening distant pages
- only summarize already confirmed incremental findings

### Priority 3: Add Explicit Completion Reasoning

Add a field such as `completion_reason` to crawler state and status API so that `done` can be interpreted correctly.

### Priority 4: Improve Quality Metrics In Status

Useful fields to add:

- `pages_discovered_total`
- `apis_discovered_total`
- `mappings_discovered_total`
- `new_pages_last_round`
- `new_apis_last_round`
- `new_mappings_last_round`

### Priority 5: Add Targeted Tests For Phased Crawler Behavior

Current tests still mainly cover server API behavior in general.

The crawler-specific phased state path needs more explicit tests for:

- phase transitions
- stop_requested
- final flush
- file existence after flush
- state-table updates

## Suggested Immediate Work Plan

If the next agent wants to continue with minimal context rebuild, the best order is:

1. read `cairn/src/axiom/server/routers/crawler.py`
2. read `docs/crawler-output-schema.md`
3. inspect the current `CrawlerExecute.md` and `CrawlerFlush.md`
4. replace YAML-first ingestion with direct structured incremental updates into the crawler state tables
5. keep the three exported YAML files as views, not the source of truth

## Final Summary

This workstream successfully delivered:

- phased crawler execution
- global crawler timing settings
- schema-v1 output model
- crawler snapshot preview in frontend
- backend crawler state tables
- state export wiring

But it did not yet fully complete the architectural transition to a truly backend-owned crawler state model the way the main graph loop already works.

That remaining shift is the highest-value next step.
