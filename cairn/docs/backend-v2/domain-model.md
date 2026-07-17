# Backend V2 Domain Model

## Project

Project is the top-level container previously represented by `project_groups`.

```text
id                  prj_001
title
description
status              active | archived
created_at
updated_at
```

Project owns Knowledge, Credentials, Tasks, and an aggregate vulnerability view.

## Task

Task is one penetration-test run previously represented by `projects`.

```text
id                  t_001
project_id
title
origin_host
origin_scheme       nullable
origin_port         nullable
origin_input
goal
status
phase
bootstrap_enabled
guard_enabled
external_report_enabled
scope_enabled
scope_json
worker_pool_json
legacy_readonly
reason_worker       nullable
reason_heartbeat_at nullable
created_at
updated_at
completed_at        nullable
```

One Task has exactly one Origin. New Origins accept a domain/IP with optional scheme and port.

## Fact

```text
id                  f001, task-local
task_id
description         concise confirmed summary
archived
created_at
```

Origin and Goal remain addressable facts for graph compatibility, but Task also stores structured origin and goal fields.

## Intent

```text
id                  i001, task-local
task_id
todo_id             nullable only for Bootstrap
description
status
creator
worker              nullable
worker_pool_json
targets_json
execute_timeout_seconds
conclude_timeout_seconds
last_heartbeat_at
retry_count
next_retry_at
last_failure_kind
to_fact_id          nullable
created_at
concluded_at        nullable
archived
```

`intent_sources` links each Intent to one or more source Facts.

Reason-created ordinary Intents require:

- one linked Todo
- one or more valid source Facts
- one or more concrete domain/IP targets
- execute and conclude timeouts

## Todo

```text
id                  td001, task-local
task_id
title
category
theme
priority            0..100
status
created_by
status_reason
created_at
updated_at
completed_at        nullable
archived
```

Categories:

```text
recon
authentication
authorization
injection
business_logic
file
network
configuration
client_side
other
```

`todo_sources` stores typed references. `todo_intents` is derivable from `intents.todo_id` and does not need a second writable relation.

## ReasonWorkspace

```text
id
task_id
status              open | committed | abandoned
context_revision
draft_json
worker
failure_reason
created_at
updated_at
committed_at        nullable
```

A committed Workspace is immutable. Formal Todo and Intent rows are created/updated in the same transaction that marks the Workspace committed.

## Artifact

```text
id                  a001, task-local
task_id
intent_id           nullable
artifact_type
path
summary
content_hash
size
created_by
created_at
archived
```

Required Bootstrap artifacts:

```text
i001_asset_inventory.md
i001_initial_leads.md
i001_findings.md
```

Required Explore artifacts:

```text
{intent_id}_findings.md
{intent_id}_leads.md
```

## ProjectKnowledge and TaskHint

ProjectKnowledge replaces the ambiguous project-level blackboard name. TaskHint is task-specific information created with or after the Task.

Both are available to Bootstrap, Reason, and Explore. Todo may cite either as a source.

## Credential

```text
id
project_id
label
auth_type
credentials_json     plaintext by product decision
domains_json
notes
active
created_at
updated_at
```

All Explore workers in the Project may use all active Credentials through `axiom-tool credential` commands.

## Vulnerability

AXIOM stores a platform-neutral vulnerability model:

```text
id
task_id
intent_id
title
type
severity
description
prerequisites
impact
reproduction_steps_json
remediation
curl_command
evidence_json
affected_target
status
source
reported_by
created_at
updated_at
archived
```

External platform adapters map this model to KK/Mozzie-specific payloads. External `project` is resolved at report time and belongs to the report attempt, not the Vulnerability row.

## ExternalReportAttempt

```text
id
vulnerability_id
platform
resolved_project
payload_json
status              pending | success | failed
attempt_count
last_error
response_json
created_at
updated_at
```

## AuditEvent

Append-only event record for Task/Intent/Reason/Worker/Guard/Tool/report actions. It preserves retry and replacement history while Intent stores only current execution state.

## ContextRevision

```text
id/revision
task_id
graph_path
todo_path
artifact_index_path
hints_path
knowledge_path
hash_manifest_json
created_at
```

Per-attempt Context Manifest points to these shared versioned resources rather than copying content.

