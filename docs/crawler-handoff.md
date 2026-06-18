# Crawler Handoff Notes

This document captures the current crawler architecture, what has already been changed, what is working, and what is still weak. It is intended as a handoff for another agent to continue improving the crawler.

## Current Goal

The crawler is no longer treated as a one-shot long-running agent that owns the final YAML files directly.

The intended architecture is now:

- phased execution: `crawl -> flush -> crawl -> ... -> final_flush`
- global crawler timing config in server settings
- crawler state owned by the backend
- exported YAML files are derived views of backend crawler state

The three exported crawler files remain:

- `site_map.yaml`
- `api_inventory.yaml`
- `page_api_map.yaml`

## Important Files

### Backend / runtime

- `cairn/src/axiom/server/routers/crawler.py`
  - phased runner logic
  - crawler meta handling
  - schema export and crawler state merge helpers

- `cairn/src/axiom/server/db.py`
  - settings columns for crawler timing
  - crawler state tables:
    - `crawler_pages`
    - `crawler_apis`
    - `crawler_page_maps`

- `cairn/src/axiom/cli.py`
  - CLI entrypoint `crawler-supervisor`

### Prompts

- `cairn/src/axiom/dispatcher/prompts/other/CrawlerExecute.md`
- `cairn/src/axiom/dispatcher/prompts/other/CrawlerFlush.md`

### Frontend

- `cairn/src/axiom/server/static/index.html`
  - crawler status card
  - crawler snapshot buttons
  - YAML preview modal

### Schema / docs

- `docs/crawler-output-schema.md`

## Current Runtime Model

### Status fields

`/project-groups/{group_id}/crawler` now returns phase-aware fields:

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
- `sitemap_exists`
- `api_list_exists`
- `api_page_map_exists`

### Timing config

Global settings were added to `/settings` and the settings page:

- `crawler_active_window_seconds`
- `crawler_flush_window_seconds`
- `crawler_total_timeout_seconds`

Defaults currently:

- active: `180`
- flush: `45`
- total: `1800`

### Supervisor process

The crawler is now started through a dedicated CLI runner instead of a background thread:

- `axiom crawler-supervisor --project-id <group_id>`

This is launched from `start_crawler()` in `crawler.py` via:

- `uv run --project <cairn-root> axiom crawler-supervisor --project-id pg_xxx`

This is intentionally closer to the stable `bootstrap/reason/explore` process model.

## Crawler State Store

The backend now has three crawler state tables.

They are keyed by `group_id`.

### `crawler_pages`

- `group_id`
- `page_id`
- `data_json`
- `updated_at`

### `crawler_apis`

- `group_id`
- `api_id`
- `data_json`
- `updated_at`

### `crawler_page_maps`

- `group_id`
- `map_id`
- `data_json`
- `updated_at`

### Important helper functions in `crawler.py`

- `_persist_schema_files_into_state(group_id, ai_dir)`
- `_export_state_to_schema_files(group_id, ai_dir, target_url)`
- `_upsert_crawler_state_row(...)`
- `_crawler_state_id_column(...)`

Current behavior:

- after `flush` / `final_flush`, if the schema files exist, backend ingests them into state store
- backend then re-exports the three YAML files from that state store

This means exported YAML is no longer meant to be the sole source of truth.

## What Was Learned From Main Loop (`bootstrap/reason/explore`)

The stable main loop does **not** let the agent maintain a large YAML file directly.

Instead:

- the agent returns a very small structured result
- backend writes authoritative state to DB
- `/export` renders YAML from DB state

This is the conceptual model the crawler has been moving toward.

Files that were read to confirm this:

- `cairn/src/axiom/dispatcher/tasks/bootstrap.py`
- `cairn/src/axiom/dispatcher/tasks/reason.py`
- `cairn/src/axiom/dispatcher/tasks/explore.py`
- `cairn/src/axiom/dispatcher/protocol/client.py`
- `cairn/src/axiom/server/routers/export.py`

## What Is Working Now

### 1. Phase switching is real

`pg_006` confirmed actual transitions in logs such as:

- `phase=crawl`
- `phase=flush`
- later rounds returning to `phase=crawl`

### 2. Flush can now produce files

The flush phase is no longer purely conceptual. It can write the three schema files.

### 3. Crawler state ingestion bug was fixed

There was a bug in `_upsert_crawler_state_row()` where the code tried to derive column names dynamically and produced invalid names like:

- `crawler_page_id`
- `crawler_api_id`
- `crawler_page_map_id`

This was fixed by explicitly mapping table name to primary id column.

### 4. Snapshot preview exists in crawler UI

The crawler UI now exposes three separate snapshot preview buttons:

- view site map
- view API inventory
- view page map

Each opens the existing YAML preview modal for a single file.

The preview modal was also adjusted to prefer vertical scrolling and wrapped content instead of forcing horizontal scrolling.

## Current Quality of `pg_006`

`pg_006` is the main validation group used in recent work.

Current observations:

- exported files exist
- backend state rows exist
- the outputs are no longer skeleton-only

However, quality is still incomplete:

### `site_map.yaml`

- contains meaningful pages such as:
  - `page_home`
  - `page_portal_login`
  - `page_guest_home`
  - `page_chat_home`
  - `page_chat_channel`
  - `page_mall_home`
  - `page_mall_product`
  - `page_desk_home`
  - further pages continue beyond the first read window

- still not a complete site graph

### `api_inventory.yaml`

- contains real API entries such as:
  - `api_portal_subsystems`
  - `api_portal_auth_guest_register`
  - `api_portal_auth_guest_login`
  - `api_ads_serve`
  - `api_ads_track_impression`
  - `api_mall_cart_items`
  - `api_desk_tickets`

- parameter provenance has started to appear
- still not dense enough for full replay-quality business flow testing

### `page_api_map.yaml`

- contains useful mappings for:
  - home on-load
  - portal guest register/login
  - guest logout
  - chat send
  - mall add-to-cart denied
  - desk submit denied

- still sparse across many discovered pages

### Practical quality assessment

Rough assessment from current state:

- phase execution stability: medium-good
- file export stability: good enough
- business modeling quality: moderate
- traffic testing readiness: moderate but incomplete

The system is now much closer to a usable crawler, but not yet a complete high-fidelity business modeling crawler.

## Known Remaining Problems

### 1. Flush still carries too much responsibility

Even after phase splitting and state store introduction, `CrawlerFlush.md` is still asked to do a lot:

- infer business names
- merge incremental findings
- map actions to APIs
- derive parameter sources

This remains the main quality bottleneck.

### 2. Incremental density is still too low

The crawler now produces structured output, but the density of discovered actions / APIs / mapping entries is still lower than the likely reachable site behavior.

### 3. Completion semantics are still weak

The system still needs a better distinction between:

- exhausted
- timeout
- stop_requested
- failed

`completion_reason` has been discussed but is not yet implemented.

### 4. Round/log cleanliness still needs polish

Phase transitions work, but there may still be log ordering / round-number consistency issues in edge cases.

Not a blocker anymore, but still a cleanup area.

## Commits On This Crawler Line

Relevant commits made during this work:

- `76f8052 docs(crawler): define output schema v1`
- `c452aeb feat(crawler): switch outputs to schema v1`
- `e15fe22 fix(crawler): detect schema v1 output files`
- `f4a5b92 feat(crawler): add global timing settings`
- `bb4068f test(settings): fix crawler timing assertion`
- `fba9523 feat(crawler): run phased execute and flush loops`
- `f8838fc feat(crawler): persist phased state before export`
- `387014d fix(crawler): persist state rows correctly`
- `cf0a9b7 feat(crawler): add snapshot preview action`
- `31eab01 feat(crawler): split snapshot preview actions`
- `4ec57a7 style(crawler): stack snapshot actions vertically`
- `ffabe5b style(preview): prefer vertical YAML scrolling`

## Recommended Next Steps For Another Agent

Priority order:

1. add `completion_reason` to crawler meta/status
   - `timeout`
   - `exhausted`
   - `stop_requested`
   - `failed`

2. improve incremental quality instead of architecture again
   - enrich `page_api_map`
   - enrich `api_inventory` provenance chains
   - deepen page coverage for mall/desk/cms/ads flows

3. reduce flush prompt responsibility further if needed
   - keep it incremental
   - avoid broad re-exploration

4. add a small targeted crawler test if feasible
   - especially around state store ingestion and export generation

## Key Takeaway

The big architectural shift is done:

- phased runner exists
- backend state store exists
- exports are derived from backend state

What remains is quality refinement, not core architecture invention.
