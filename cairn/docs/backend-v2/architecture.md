# AXIOM Backend V2 Architecture

Status: accepted design baseline  
Branch: `refactor/backend-v2`  
Production baseline: `c81e1e4`

## Goals

- Refactor the FastAPI server and Dispatcher together without turning AXIOM into a distributed system.
- Keep the fact graph model while making lifecycle rules explicit and testable.
- Add a persistent Reason-owned Todo list without making Todo a schedulable unit.
- Make the Server/database the only durable source of truth and make Dispatcher restartable.
- Remove obsolete domains: asset database, depth mode, Plan, and Maintainer.
- Preserve historical tasks for read-only viewing/export; only new V2 tasks are executable.

## Deployment shape

AXIOM remains a modular monolith in one repository with two processes:

```text
axiom-api         FastAPI, domain services, repositories, migrations, static frontend
axiom-dispatcher  reconciliation scheduler, worker allocation, phase runners
```

Both processes run on one machine. SQLite remains the database. Dispatcher accesses durable state only through `/api/v1`; it never imports repositories or opens the database.

## Module layout

```text
axiom/
  api/
    v1/
  modules/
    projects/
    tasks/
    graph/              # facts, intents, hints
    todos/
    reason_workspace/
    artifacts/
    vulnerabilities/
    credentials/
    workers/
    knowledge/
    audit/
    settings/
  dispatcher/
    reconciler/
    scheduler/
    workers/
    phases/
      bootstrap/
      reason/
      explore/
    retry/
    context/
  infrastructure/
    database/
    filesystem/
    process/
    http/
  guard/
```

Modules are vertical slices. A module owns its API schemas, application service, repository, domain rules, and tests. Global `models.py`, `services.py`, and giant routers are not recreated.

## Core workflow

```text
Task Created
  -> Bootstrap special Intent
  -> Reason
  -> Explore ordinary Intent(s)
  -> Reason
  -> ...
  -> Completed / Stopped / Failed
```

- Bootstrap remains the first special Intent and is visible in the graph.
- Reason is a task-level coordinator, not an Intent.
- Explore executes ordinary Intents.
- Task completion depends only on whether Goal is achieved.
- When Goal is achieved, running Intents are cancelled and unfinished Todos remain as historical planning records.

## Scheduler architecture

Dispatcher uses a one-second reconciliation loop, not Redis/Celery.

```text
Server snapshot
  -> Reconciler calculates desired actions
  -> WorkerAllocator selects a worker
  -> Bootstrap/Reason/Explore Runner starts work
  -> Runner reports state through command endpoints
```

The Scheduler does not build prompts, parse model output, manage artifacts, or write graph data. Phase runners do not select workers or scan all tasks.

## State ownership

The Server is the sole durable source of truth for:

- Task status and current phase
- Intent status, worker, lease, retries, and final conclusion
- Reason lease and last committed context revision
- Todo state
- Reason Workspace state
- Worker cooldown state
- Artifact indexes
- Audit events

Dispatcher memory is limited to live process handles and disposable caches.

## Intent state machine

```text
pending -> claimed -> running -> concluding -> completed
                  \-> pending       # release/expired lease
                  \-> failed
pending/claimed/running/concluding -> cancelled
```

State transitions are command services, not arbitrary row updates.

## Task state and phase

Lifecycle:

```text
draft | running | stopped | completed | failed
```

Current phase:

```text
bootstrap | reason | explore | waiting | none
```

Historical migrated tasks use `legacy_readonly=true` and cannot be resumed.

## Todo model

Todo is persistent planning memory shared by all Reason rounds. It is not claimed by workers and has no lease/session/retry fields.

```text
pending -> active -> completed
                  -> rejected
                  -> obsolete
```

Rules:

- Single-level list; no parent/child hierarchy.
- One ordinary Intent links to exactly one Todo.
- One Todo may produce multiple Intents over time.
- Todo has no target field and is not Scope-validated.
- Every Todo must reference at least one Fact, Hint, ProjectKnowledge, Artifact, Vulnerability, or Origin.
- Human users and Reason can both edit Todo.
- Reason owns final interpretation of whether an Intent result completes a Todo.

Default limits:

- Initial Reason: at most 10 new Todos.
- Later Reason rounds: at most 5 new Todos.
- At most 10 Todo updates per round.
- At most 4 new Intents per round.
- At 100 unfinished Todos, Reason must reduce/resolve the list before adding more.

## Reason Workspace

Reason does not return a large Todo/Intent JSON payload in its final response. It edits a server-backed draft through `axiom-tool`:

```text
axiom-tool todo add/update/status
axiom-tool intent propose
axiom-tool reason commit
```

The Workspace stores validated draft JSON. `reason commit` validates cross-references, Scope, limits, mandatory Intent timeouts, and leases, then applies Todo and Intent changes in one transaction.

Failed or timed-out Reason runs leave an `abandoned` audit draft. They do not change formal Todo/Intent data.

## AXIOM CLI tools

AXIOM-owned model tools are CLI-only. Worker-defined MCP remains independent.

Reason tools:

```text
todo list/add/update/status
intent propose
reason commit
```

Explore tools:

```text
credential list/get
vulnerability report
artifact register
```

Environment variables provide Task, Intent, Workspace, Server URL, and Service Token. Logs may record credential IDs but never credential contents.

## Context reuse

Canonical data is stored once in the database/artifact filesystem. Context generation uses versioned shared snapshots and small per-run manifests:

```text
contexts/tasks/t_001/revisions/18/graph.yaml
contexts/tasks/t_001/revisions/18/todos.json
contexts/projects/prj_001/knowledge-v5.md
contexts/tasks/t_001/hints-v2.md
contexts/tasks/t_001/artifacts-v12.json
contexts/manifests/<attempt-id>.json
```

Manifests reference shared resources by path, revision, and content hash. They do not copy unchanged content.

## Scope

- Task Scope defaults to enabled with the exact Origin host in the allowlist.
- Subdomains require an explicit `*.example.com` entry.
- Scope matches host/domain only, not port.
- Todo is not Scope-validated.
- Scope is enforced when Reason proposes an Intent.
- Explore network calls are not blocked by Scope; they remain auditable.

## Guard

Guard is independent from Scope and reviews external operational tool calls such as shell, curl, nmap, Python, and file operations.

```text
off | observe | guarded | enforce
```

- Hard-rule matches block deterministically.
- A valid model BLOCK is enforced only in enforce mode.
- Guard model timeout/error fails open and writes a `guard_error` event.
- AXIOM internal CLI tools use deterministic phase permissions and are not judged by the Guard model.

## Persistence

- SQLite with WAL, foreign keys, and busy timeout.
- SQLAlchemy 2 Core for repositories and transactions.
- Alembic for schema versioning and migrations.
- Pydantic for API and command schemas.
- Synchronization remains the default; async is used only at genuinely concurrent I/O boundaries.

## API style

New endpoints use `/api/v1`. Old endpoints are removed at cutover; there is no compatibility adapter.

Lifecycle writes use command endpoints, for example:

```text
POST /api/v1/intents/{id}/claim
POST /api/v1/intents/{id}/heartbeat
POST /api/v1/intents/{id}/release
POST /api/v1/intents/{id}/conclude
POST /api/v1/tasks/{id}/reason/claim
POST /api/v1/tasks/{id}/reason/commit
POST /api/v1/tasks/{id}/stop
POST /api/v1/tasks/{id}/resume
POST /api/v1/tasks/{id}/complete
```

No general command-id or optimistic-version mechanism is introduced. Existing lease-owner conflict checks remain. A Reason Workspace itself can only be committed once.

## Removed or disabled components

Removed:

- Asset database/API/frontend/reason baseline script
- Depth mode
- Plan phase and prompts
- Maintainer model phase
- Inline Task request-header credentials

Disabled by default through a feature flag:

- Crawler frontend entry
- Crawler router registration
- Crawler Dispatcher logic

Crawler code and tables remain for future reactivation.

## Observability

- Task graph right panel shows Todo, not low-level logs.
- Append-only audit events remain available in an execution-audit page.
- Large stdout/stderr is stored as files; the database stores summary and index.
- Claim, heartbeat expiry, retry, worker replacement, Guard decision, CLI tool call, and external report attempt are auditable.

## Security boundary

- Local frontend access remains login-free while bound to localhost.
- Dispatcher and `axiom-tool` mutating/read-sensitive calls require a Service Token.
- Credentials are intentionally stored in plaintext by product decision.
- All Explore workers in a Project may read all active Project credentials.

