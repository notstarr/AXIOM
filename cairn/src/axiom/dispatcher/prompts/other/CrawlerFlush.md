# Task
You are in the crawler flush phase.

Your job in this phase is NOT to continue broad exploration.
Your job is to persist everything already discovered into the schema files.

You must update these files in `{ai_dir}` according to:
- `repo-root/docs/crawler-output-schema.md`

Target files:
- `site_map.yaml`
- `api_inventory.yaml`
- `page_api_map.yaml`

# Flush Phase Rules
- Do not open distant new branches just to keep exploring
- You may inspect the current page, read the existing YAML, and read the latest captured traffic
- You may perform only the minimum local inspection needed to finish writing records cleanly
- Every page already reached in the current session but not yet written must be written now
- Every API already observed in traffic but not yet written must be written now
- Every user action already performed but not yet mapped must be written now
- If a request failed (401/403/404/500/validation failure), record it as a failed action or failed API observation instead of dropping it

Required flush checklist:
- If `site_map.yaml` does not exist, create it now with schema v1 skeleton
- If `api_inventory.yaml` does not exist, create it now with schema v1 skeleton
- If `page_api_map.yaml` does not exist, create it now with schema v1 skeleton
- If the entry page has been visited but `site_map.yaml.pages` is empty, write the entry page immediately
- If any page has been visited in this session, `site_map.yaml.pages` must not remain empty when you finish flush
- If any API has been seen in traffic, `api_inventory.yaml.apis` must not remain empty when you finish flush
- If any user action has been performed, `page_api_map.yaml.mappings` must not remain empty when you finish flush
- If you know less than expected, write partial but valid schema records instead of leaving the files empty

# Hard Requirement
Do not leave the three schema files as stale snapshots if newer evidence already exists in the browser session or traffic log.

Do not finish this phase with skeleton-only files if the browser has already visited one or more real pages.

Minimum acceptable result for this flush phase:
- at least one concrete page record in `site_map.yaml`
- at least one concrete action or on-load mapping in `page_api_map.yaml` once actions or page-load traffic exist
- at least one concrete API record in `api_inventory.yaml` once API traffic exists

# Environment
- Browser automation: `browser-use --cdp-url http://localhost:9223 --session {intent_id} <command>`
- Traffic query: `python3 repo-root/tools/mitm/proxy_get_traffic.py`
- Output directory: `{ai_dir}`
- Schema reference: `repo-root/docs/crawler-output-schema.md`
- Activate venv before any command: `source ~/browser-use-env/bin/activate`

# Output
Return only one raw JSON object:

```json
{"accepted": true, "data": {"description": "整理阶段已完成，新的页面、动作与接口证据已写入 schema 文件。"}}
```

If you cannot produce concrete schema records, do not pretend flush succeeded. Write the records first.

# Context
## Session
```
{intent_id}
```

## Target
```
{intent_description}
```
