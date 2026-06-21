# Task
You are in the crawler flush fallback phase.

This phase exists only because the crawl phase failed, timed out, or returned invalid output. Your job is to recover the best confirmed **incremental crawler payload** from evidence that already exists.

This phase overrides any earlier instruction in the same session that told you to continue exploring. Do not keep crawling.

# Flush Fallback Rules
- Do not open new branches
- Do not continue normal exploration
- Do not use browser automation for fresh exploration
- Use only already-confirmed evidence from this round:
  - latest captured traffic
  - screenshots or local artifacts
  - current logical crawler state
- If you can recover a valid incremental payload, return it
- If you cannot recover any concrete increment, return a rejection payload

# Environment
- CDP helper is available but should NOT be used for fresh exploration in this phase:
  `python3 tools/crawler/cdp_ctl.py --tab-id {intent_id} <command>`
  (only `url` / `state` are acceptable if you need to confirm where the browser ended up)
- Traffic query: `python3 tools/mitm/proxy_get_traffic.py`
- Output directory for screenshots/artifacts: `{ai_dir}`
- Schema reference: `docs/crawler-output-schema.md`
- Do NOT use `browser-use`.

# What Counts As Recoverable Increment
- a newly discovered page
- a newly observed API
- a newly confirmed page-to-API mapping
- a failed action/API outcome such as 401/403/validation failure

If evidence is partial, return partial but valid records. Do not invent missing facts.

# Output
Return only one raw JSON object.

Success example:
```json
{
  "accepted": true,
  "data": {
    "description": "保底阶段恢复了工单提交失败相关增量。",
    "pages": [
      {
        "id": "page_desk_ticket_new",
        "name": "新建工单页",
        "url": "https://desk.redhaze.top/desk/tickets/new",
        "path": "/desk/tickets/new",
        "page_type": "form"
      }
    ],
    "apis": [
      {
        "id": "api_desk_tickets",
        "method": "POST",
        "path": "/api/desk/tickets",
        "status_codes_seen": [401]
      }
    ],
    "mappings": [
      {
        "id": "map_desk_ticket_new",
        "page_id": "page_desk_ticket_new",
        "page_name": "新建工单页",
        "page_url": "https://desk.redhaze.top/desk/tickets/new",
        "on_load": [],
        "actions": [
          {
            "action_id": "action_submit_ticket",
            "action_name": "提交工单",
            "trigger_type": "form_submit",
            "trigger_label": "提交工单",
            "target_hint": "工单创建表单",
            "result": {
              "navigation": false,
              "status": "denied"
            },
            "apis": [
              {
                "api_id": "api_desk_tickets",
                "method": "POST",
                "path": "/api/desk/tickets"
              }
            ]
          }
        ]
      }
    ]
  }
}
```

Failure example:
```json
{"accepted": false, "reason": "no_recoverable_increment"}
```

Requirements:
- `description` is required on success
- At least one of `pages` / `apis` / `mappings` must be non-empty on success
- Return only the incremental records recoverable from this failed crawl round

# Context
## Session (CDP target id for your dedicated tab)
```
{intent_id}
```

## Target
```
{intent_description}
```
