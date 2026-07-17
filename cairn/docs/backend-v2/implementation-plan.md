# Backend V2 Implementation Plan

## Phase 0: baseline

- [x] Audit current production workspace.
- [x] Exclude runtime output, databases, handoff files, and one-off scripts.
- [x] Create production checkpoint commit `c81e1e4`.
- [x] Create branch `refactor/backend-v2`.
- [x] Record current full-suite baseline: 169 passed, 6 known credential/Guard failures.

## Phase 1: characterization and skeleton

- Add architecture dependency rules.
- Add characterization tests for current Bootstrap/Reason/Explore, leases, retries, Scope, and task lifecycle.
- Add SQLAlchemy Core and Alembic dependencies/configuration.
- Create infrastructure database package and module skeletons.
- Add feature flags with Crawler disabled by default.

## Phase 2: V2 schema and repositories

- Implement Project and Task repositories/services.
- Implement Fact/Intent/Hint/Knowledge repositories/services.
- Implement Todo and Reason Workspace.
- Implement Artifact, Credential, Vulnerability, external report attempt, and AuditEvent.
- Add migration scripts and verification reports.

## Phase 3: `/api/v1`

- Implement read APIs and command state-transition APIs.
- Add Service Token dependency for Dispatcher/CLI endpoints.
- Add contract tests independent of legacy routes.
- Keep legacy routes only until the frontend and Dispatcher V2 clients are ready on the branch.

## Phase 4: CLI and context

- Implement `axiom-tool` CLI.
- Add deterministic phase permissions.
- Add Reason Workspace draft operations and commit validation.
- Add shared context revisions and per-attempt manifests.
- Move durable files from `/tmp` to `AXIOM_DATA_DIR`.

## Phase 5: Dispatcher

- Add pure Reconciler action calculation tests.
- Add WorkerAllocator and persistent cooldown/retry policy.
- Add ProcessManager.
- Move Bootstrap, Reason, and Explore into isolated runners.
- Remove Plan and Maintainer routing.
- Verify Dispatcher restart recovery.

## Phase 6: frontend

- Migrate all calls to `/api/v1`.
- Rename Project/Task consistently.
- Replace task-detail log panel with Todo.
- Add Todo editing/filtering.
- Remove asset pages and depth mode.
- Hide Crawler by feature flag.
- Update vulnerability form to the neutral model and add external payload preview/report action.

## Phase 7: cleanup and acceptance

- Delete legacy routers/models/services after V2 callers are complete.
- Delete asset, Plan, depth, and Maintainer implementation/tests.
- Run migration against a production DB copy.
- Run unit, integration, fault, frontend, and safe production E2E tests.
- Perform backed-up cutover and acceptance.

## Test policy

Every migrated domain requires:

- repository tests
- service/state-machine tests
- API contract tests
- migration tests
- negative/conflict tests

Dispatcher requires deterministic unit tests for action calculation. Model/process integration is covered separately with fakes before production E2E.
