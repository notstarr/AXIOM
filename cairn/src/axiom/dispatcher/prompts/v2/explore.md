# AXIOM Explore

Execute Task `{task_id}` Intent `{intent_id}`:

`{intent_description}`

- Context Manifest: `{manifest_path}`
- Writable run directory: `{run_dir}`

Read the Context Manifest and all relevant referenced resources before testing. Follow the Intent, but autonomously obtain prerequisite IDs/tokens through related in-scope requests when necessary. Do not create Todos or Intents.

Write and register two Markdown artifacts for this Intent:

1. `findings` — verified results and exact evidence; report each confirmed vulnerability through `axiom-tool vulnerability report`.
2. `leads` — concrete follow-up directions and uncovered attack-surface details for the next Reason round.

Use `axiom-tool credential` only when credentials are needed. Store files under `{run_dir}` and register them with `axiom-tool artifact register`.
