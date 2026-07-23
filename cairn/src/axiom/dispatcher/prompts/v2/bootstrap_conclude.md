# Conclude Bootstrap

Task `{task_id}`, Intent `{intent_id}`.

All human-readable output must be written in Chinese. The three Markdown strings
inside `bootstrap_artifacts.json` and the final graph summary must use Chinese for
titles, evidence analysis, leads, findings, and conclusions. Commands, paths,
URLs, identifiers, JSON field names, and raw protocol evidence may remain in their
original form.

The exploration budget may have expired. Conclude immediately from the evidence
already collected. Do not perform new network exploration, source-tree searches,
environment archaeology, dependency inspection, or shell commands. In particular,
do not call `bash`: this is a close-out turn, not another investigation.

Read `{manifest_path}`, its `phase_context`, and inspect `{run_dir}`. If a compact
execute transcript exists at `{evidence_path}`, read that file as the execute
evidence. Compact index previews are not complete evidence; do not expand the
attack surface or inspect unrelated files. The files still missing are:

`{missing_artifacts}`

在本阶段开始以及执行下一步收口操作前，先查看当前会话自己的 Skill 目录（Pi
通过 `--skill` 暴露的目录）和可用 Skill 描述。只查阅可能帮助解释已有证据或
整理产物的 Skill；如果存在相关 Skill，先阅读对应的 `SKILL.md`，再使用该
Skill。不需要、也不要一次性读取全部 Skill，更不能以查阅 Skill 为理由重新
开始探索。

Submit all missing Bootstrap content in **one** JSON file:

`{run_dir}/bootstrap_artifacts.json`

It must be a valid JSON object with these three non-empty string fields:

```json
{
  "asset_inventory": "# ...",
  "initial_leads": "# ...",
  "findings": "# ..."
}
```

Include already-known evidence in all three fields when the bundle is being
created for the first time. AXIOM Runner atomically expands the bundle into:

- `{run_dir}/asset_inventory.md`
- `{run_dir}/initial_leads.md`
- `{run_dir}/findings.md`

Never create empty or unsupported content merely to satisfy the contract. Keep
the three fields compact and evidence-based. Write the one bundle before
returning; do not spend time polishing a prose response. Do not edit the AXIOM database directly
or write Context snapshot files. AXIOM will expand, register,
and validate the files after you return; no registration command is required.

Before preserving or reporting any vulnerability from the collected evidence,
read the available `whatisvul` and `vuln-report` Skills. Apply their actual
impact and proof standards. Configuration observations, missing security
headers, version strings, HTTP 200 alone, and theoretical risks belong in
`initial_leads`, not `findings`. CORS is not a confirmed vulnerability without
a demonstrated browser-compatible cross-origin read or state change. Do not
promote a weak claim merely because it appeared in the execute transcript.

Never copy complete JWTs, Authorization/Cookie values, API keys, passwords,
cloud temporary credentials, organization credentials, or Secrets into the
bundle or final summary. Preserve only the field name and use `[REDACTED]`.

Return one concise factual summary suitable for a graph Fact. Mention coverage, the most important leads, and confirmed findings. Do not output JSON and do not claim unverified vulnerabilities.
