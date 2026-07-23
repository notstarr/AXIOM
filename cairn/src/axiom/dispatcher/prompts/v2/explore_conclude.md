# Conclude Explore

Task `{task_id}`, Intent `{intent_id}`: `{intent_description}`

All human-readable output must be written in Chinese. The contents of
`findings.md`, `leads.md`, vulnerability reports, and the final graph summary must
use Chinese for titles, evidence analysis, conclusions, and follow-up directions.
Commands, paths, URLs, identifiers, and raw HTTP/protocol evidence may remain in
their original form.

The exploration budget may have expired. Conclude immediately from the persisted
session and evidence already collected. Do not perform new network exploration or
source-tree searches.

Read `{manifest_path}`, its `phase_context`, and inspect `{run_dir}`. If
`{evidence_path}` is a non-empty existing path, read it before rewriting either
artifact: it is the bounded transcript of the Execute turn and is authoritative
about which tools actually ran and what they returned. Do not claim that a tool
or test was not executed when that transcript or a durable output file proves it
ran. Compact index previews are not complete evidence; use only the evidence
already present in the resumed session, manifest, Execute transcript, and run
directory. Do not expand the attack surface or inspect unrelated files. Write
these exact non-empty Markdown files with the available evidence:

- `{run_dir}/findings.md`
- `{run_dir}/leads.md`

在本阶段开始以及执行下一步收口操作前，先查看当前会话自己的 Skill 目录（Pi
通过 `--skill` 暴露的目录）和可用 Skill 描述。只查阅可能帮助解释已有证据或
整理产物的 Skill；如果存在相关 Skill，先阅读对应的 `SKILL.md`，再使用该
Skill。不需要、也不要一次性读取全部 Skill，更不能以查阅 Skill 为理由重新
开始探索。

Never create an empty or placeholder artifact merely to satisfy the contract. Do not edit the AXIOM database directly or write Context snapshot files. AXIOM will register and validate the files after you return; no registration command is required. Report any confirmed vulnerability already proven in the collected evidence.

Before preserving or reporting a claimed vulnerability, read the available
`whatisvul` and `vuln-report` Skills and apply their proof and severity rules.
Do not convert a configuration observation, HTTP 200 response, missing security
header, version string, or theoretical risk into a confirmed finding. CORS is
not confirmed without a demonstrated browser-compatible cross-origin read or
state change. Evidence that does not meet the standard must remain an explicitly
unconfirmed item in `leads.md`.

Return exactly one JSON object and nothing else:

```json
{"accepted": true, "data": {"description": "本次 Intent 已确认的结果摘要，引用 findings.md 和 leads.md"}}
```

The `description` must be a concise Chinese summary of only the evidence already present
in this run. It must reference both `findings.md` and `leads.md`, and must
state the tested scope, the most important positive or negative result, whether
a confirmed vulnerability was produced, and the principal remaining lead. A
description that only points to the two files is invalid. Do not include `<think>`
content, plans, waiting/stop acknowledgements, timeout or heartbeat state, or conclusions
from another Intent. Do not return plain text, Markdown, or explanatory filler.
