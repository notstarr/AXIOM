# Production Migration Plan

## Compatibility boundary

- Historical records remain viewable and exportable.
- Historical Tasks are not resumable.
- New Server, Dispatcher, frontend, and API are deployed together.
- Old API routes are not retained.
- Rollback restores the complete pre-migration backup; reverse migrations are not used.

## Source to target mapping

```text
project_groups       -> projects_v2
projects             -> tasks_v2
facts                -> facts_v2
intents              -> intents_v2
intent_sources       -> intent_sources_v2
hints                -> task_hints
blackboard_items     -> project_knowledge
vulns                -> vulnerabilities_v2
credentials          -> credentials_v2
task_events          -> audit_events
tool_hook_events     -> guard_events/audit_events
tool_executions      -> audit_events plus file indexes
assets               -> legacy asset Artifact per Project/Task
```

Crawler tables remain unchanged and the feature is disabled by default.

## ID mapping

Migration creates stable mapping tables/log output:

```text
pg_007   -> prj_007
proj_093 -> t_093
f001     -> f001 within t_093
i001     -> i001 within t_093
```

Task-local Fact and Intent IDs remain readable. New Todo and Artifact counters begin after any migrated references.

## Historical task policy

All migrated Tasks receive:

```text
legacy_readonly = true
phase = none
```

Existing active/stopped tasks are mapped to a non-running historical status suitable for viewing. No lease or worker process is recovered.

## Assets archival

The structured asset database is removed. Before dropping the V2-facing asset API, migration exports each Project's historical assets to JSON Artifact files and creates Artifact indexes. The original legacy table may remain in the backup database but is not queried by V2.

## Filesystem migration

Source:

```text
/tmp/axiom-files
```

Target:

```text
$AXIOM_DATA_DIR/projects
```

Migration copies files, computes content hashes, registers known Bootstrap/Explore artifacts, and records missing paths without failing the entire database migration. Source files are not deleted during cutover.

## Cutover procedure

1. Stop Server and Dispatcher.
2. Copy the production SQLite database and artifact directory to timestamped backups.
3. Verify backup hashes and that SQLite can open the copied database.
4. Run Alembic migration against a working copy.
5. Run migration verification:
   - Project/Task counts
   - Fact/Intent/Hint/Vulnerability counts
   - source and target references
   - orphan checks
   - artifact index and missing-file report
6. Start V2 Server only and run API/schema health checks.
7. Start V2 Dispatcher.
8. Run one safe end-to-end Task.
9. Enable normal Task creation after acceptance.

## Rollback

If any acceptance gate fails:

1. Stop V2 processes.
2. Restore the database backup and original artifact path/configuration.
3. Switch code to commit `c81e1e4`.
4. Start the old Server and Dispatcher.
5. Verify production health before allowing new work.

## Migration acceptance gates

- No orphan Task/Fact/Intent/Todo/Vulnerability references.
- Historical graph and vulnerability views load.
- Historical export succeeds.
- No historical Task is claimable.
- New Task creation generates Bootstrap Intent `i001`.
- New Task completes Bootstrap -> Reason -> Explore -> Reason.
- Todo survives Dispatcher restart.
- Retry counters/cooldowns survive Dispatcher restart.
- Stop/resume works for new Tasks.
- Goal completion cancels remaining Intents.

