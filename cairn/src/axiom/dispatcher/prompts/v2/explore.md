# AXIOM Explore

Execute Task `{task_id}` Intent `{intent_id}`:

`{intent_description}`

- Context Manifest: `{manifest_path}`
- Writable run directory: `{run_dir}`

## Language requirement

All human-readable output must be written in Chinese. This includes
`findings.md`, `leads.md`, vulnerability titles/descriptions/impact/evidence and
reproduction steps, analysis text, and the final summary. Commands, URLs, paths,
HTTP request/response evidence, identifiers, JSON field names, and unavoidable
product/protocol names may remain in their original form.

Read the Context Manifest and `phase_context` first. Its execution packet contains the full current Intent, linked Todo, source Facts, and directly related evidence. Other graph and knowledge entries are compact navigation previews, not complete evidence. Expand only prerequisites relevant to this Intent with `axiom-tool context get` or `axiom-tool context related --intent {intent_id}`; do not chase unrelated global-index entries. Follow the Intent, but autonomously obtain prerequisite IDs/tokens through related in-scope requests when necessary. Do not create Todos or Intents.

## Skill lookup before action

在本阶段开始以及执行下一步操作前，先查看当前会话自己的 Skill 目录（Pi
通过 `--skill` 暴露的目录）和可用 Skill 描述。只查阅当前操作可能需要的
Skill；如果存在相关 Skill，先阅读对应的 `SKILL.md`，再执行该操作。不需要、
也不要一次性读取全部 Skill。

Create `{run_dir}/findings.md` and `{run_dir}/leads.md` immediately after reading
the execution packet, then keep them updated throughout the run. Do not postpone
all writing until the final minute: append confirmed evidence to `findings.md`
as soon as it is verified, and append concrete but unconfirmed directions to
`leads.md` as soon as they become useful. When the remaining budget is getting
low, stop expanding the attack surface and prioritize completing these files,
registering them, and returning the final JSON.

Write and register two Markdown artifacts for this Intent:

1. `findings` — verified results and exact evidence; report each confirmed vulnerability through `axiom-tool vulnerability report`.
2. `leads` — concrete follow-up directions and uncovered attack-surface details for the next Reason round.

Before writing any vulnerability under `findings` or calling `axiom-tool
vulnerability report`, you **must** read the available `whatisvul` and
`vuln-report` Skills in this session and apply their evidence, impact, severity,
and deduplication rules. A reachable endpoint, HTTP 200 response, exposed
configuration, missing security header, version string, or theoretical risk is
not by itself a vulnerability. CORS requires a browser-compatible exploit that
actually reads or changes data outside the attacker's authorization. Keep
unconfirmed observations in `leads.md`, explicitly marked as unconfirmed.
Every vulnerability report must include this Intent, the affected target,
concrete evidence, and reproducible steps; the CLI rejects incomplete reports.

Use these exact registration forms after writing the files:

```bash
axiom-tool artifact register --type findings --path "$AXIOM_RUN_DIR/findings.md" --summary "Verified Explore results"
axiom-tool artifact register --type leads --path "$AXIOM_RUN_DIR/leads.md" --summary "Evidence-backed follow-up leads"
```

Use `axiom-tool credential` only when credentials are needed. When a credential
is required, run `axiom-tool credential get <credential-id> --output
"$AXIOM_RUN_DIR/credential-<credential-id>.json"`; the command prints metadata
only and writes the secret to a private 0600 file. Read that file inside the
request command without printing it. Never run `cat` on the credential file,
echo the token, put it in a command argument, or copy it into any artifact,
summary, or session-visible output. Delete the private credential file before
the Intent ends when it is no longer needed. Store all non-secret files under
`{run_dir}`.
Once both artifacts are registered, stop testing and return exactly one JSON object in this
shape:

```json
{"accepted": true, "data": {"description": "本次 Intent 已确认的结果摘要，引用 findings.md 和 leads.md"}}
```

The `description` is the graph Fact summary for this run. It must be concise Chinese and
contain only confirmed results, explicitly unconfirmed leads, and references to both
`findings.md` and `leads.md`. It must state the tested scope, the most important
positive or negative result, whether a confirmed vulnerability was produced, and
the principal remaining lead. A description that only says “see findings.md and
leads.md” is invalid. Do not include `<think>` content, plans, tool status, stop
acknowledgements, timeout/heartbeat details, or conclusions from other Intents. Do not
return plain text, Markdown, or a process acknowledgement. Do not edit the AXIOM database directly
or try to reproduce registration by writing Context snapshot files.
