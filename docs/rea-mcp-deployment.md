# REA MCP on the operator Mac (deployment)

REA (`morluto/rea`, MIT, `rea-agents@6.8.0`) runs on the operator's Mac and is
reached from the AXIOM VPS through a reverse SSH tunnel. REA is the static
analysis surface for client-side work — package inventory, APK signatures,
component declarations, JS/Electron graphs, local storage — and deliberately
runs where the *samples* are, not where the *server* is.

## Why it is not on the VPS

Measured on `124.220.165.118` (4 vCPU / 3.7 GB / 9.1 GB free):

| Constraint | Measurement | Consequence |
|---|---|---|
| Memory | 355 MB RSS per MCP process at `tools/list` | `global_max_active_runs=15` would need ~5.3 GB on a 3.7 GB host |
| Disk | 161 MB `node_modules` + 300 MB JDK + 25 MB jadx | would leave the VPS near full |
| GitHub | `github.com` unreachable; release assets time out | engines could not be provisioned there at all |
| macOS tooling | `hdiutil`, `assetutil`, `plutil`, `codesign` | DMG/iOS/macOS analysis is `unsupported_host` on Linux |

The Mac has neither limit: 373 GB free, arm64, and GitHub reachable for Node
(though GitHub release assets still need a proxy — see "Gotchas").

## Topology

```
┌─ Mac (arm64, macOS 15.6.1) ──────────┐      ┌─ VPS 124.220.165.118 ────────┐
│ rea mcp --http 127.0.0.1:8787/mcp    │◄─────│ 127.0.0.1:18787              │
│ launchd: com.xrayai.rea.mcp          │ ssh  │ axiom worker → MCP client    │
│ launchd: com.xrayai.rea.tunnel       │ -R   │                              │
└──────────────────────────────────────┘      └──────────────────────────────┘
```

The Mac dials out, so this survives NAT and a changing public IP. The remote
listener is bound to `127.0.0.1` on the VPS because `GatewayPorts` stays `no`;
nothing is exposed on the VPS public interface.

## Components

| Path | Role |
|---|---|
| `~/.local/opt/node24` | Node v24.22.2 (`^24.22.1` satisfies REA's engine range) |
| `~/.local/opt/jdk21` | Temurin JDK 21.0.13 — JADX needs a JRE, macOS ships none |
| `~/.local/share/rea/android/jadx-headless-mcp-0.7.1-all.jar` | `REA_JADX_MCP_JAR` |
| `~/.local/lib/rea/apktool-3.0.3.jar` + `~/.local/bin/apktool` | `REA_APKTOOL_COMMAND`; 3.0.3 is past CVE-2026-39973 (fixed 3.0.2) |
| `~/.config/rea/env.sh` | single source of truth for engine paths and catalog profiles |
| `~/.local/bin/rea-mcp-serve.sh` | loopback guard + `exec rea mcp --http` |
| `~/.local/bin/rea-tunnel.sh` | `ssh -N -R 127.0.0.1:18787:127.0.0.1:8787` |
| `~/Library/LaunchAgents/com.xrayai.rea.{mcp,tunnel}.plist` | `KeepAlive`, 10 s throttle |
| `~/.local/share/rea/logs/` | `mcp-server.log`, `tunnel.log` |

Everything lives under `~/.local`; no sudo and no system Node/Java is touched.
The pre-existing `com.xrayai.axiom.*` LaunchAgents and `~/.axiom-deploy` are
untouched.

## Catalog size control

`tools/list` advertises **229 tools**. Measured payload:

| Profile | Bytes | ≈ tokens |
|---|---|---|
| default (full input, full output) | 3,504,976 | 973,600 |
| `REA_MCP_OUTPUT_SCHEMA_PROFILE=off` | 302,214 | 83,900 |

`env.sh` sets `REA_MCP_INPUT_SCHEMA_PROFILE=compact` and
`REA_MCP_OUTPUT_SCHEMA_PROFILE=off`. Without them the worker prompt absorbs a
million tokens of schema before doing any work. `off` is a legal value; note
`compact` is **not** accepted for the output profile (only `full` | `off`).

## Verification performed

```
rea doctor                        node ok, host ok, jadx ok (others optional)
rea inspect-apk-signature         v1/v2/v3 signers, cert SHA-256, rotation: 0.56 s
rea inspect-android-package       10,765 classes, 1,119 resources, 61 components: 5.2 s
MCP initialize over tunnel        HTTP 200
axiom discover_mcp_server         tool_count 229, server rea 6.8.0, 1.2 s
mcp_tools rows (DB)               229
tool call from axiom venv         inspect_apk_signature, isError=false, 0.7 s
kill tunnel → launchd restored    remote 18787 listening again
kill server → launchd restored    local 8787 listening, tunnel still 200
```

## Gotchas

**GitHub release assets are unreachable from both hosts.** `github.com` times
out; `gh-proxy.com` / `ghproxy.net` truncate a 42 MB JAR mid-stream. The JADX
JAR was therefore downloaded once on the VPS (where the connection held) and
copied over with `scp`. apktool came from Maven Central
(`org/apktool:apktool-cli:3.0.3`), which is reachable directly. JDK came from
the Tsinghua Adoptium mirror, checksum cross-checked against the Adoptium API.

**Not installed:** Hopper (macOS-only, commercial), Ghidra (1.5 GB, GitHub-only
download), IDA, Kuna. Their tools return `missing_analysis_engine`; native
decompilation is unavailable until one is provisioned. Everything needed for
package-level client work is already working.

**`rea setup` was not run.** It rewrites `~/.dsh`, `~/.pi`, `~/.qoder` and
`~/.workbuddy` registrations. This deployment serves the VPS over HTTP instead,
so local agent registrations stay untouched. `rea doctor` therefore still
reports `registration:*` and `skill:identity` as drift — expected, not a fault.

## Behavioural notes for the AXIOM integration

**One client at a time.** REA's HTTP transport is stateful and serves a single
session; a new `initialize` *replaces* the current session and cancels its
in-flight calls. AXIOM's `POST /mcp/servers/{id}/discover` opens a session, so
**never re-discover while a worker is using rea**.

**Default delivery is a summary.** `inspect_apk_signature` and similar tools
return `normalized_result.kind == "summary"` plus `parent_evidence_id`; the
complete Evidence stays in the server's session and is read back with
`inspect_analysis_view`. The summary already carries the load-bearing facts
(scheme presence, certificate SHA-256, `posture`, `unknowns`). Pass
`detail: "complete"` when a worker needs the full record inline — but note the
complete record only remains reachable while that same MCP session lives.

**Tools are session-stateful.** Several tools are declared
`requiresActiveTarget: true` and their input schema has *no* `path` argument
(`inspect_artifact` accepts only `detail` and `integrity_policy`). A caller
must `open_binary` first. This is only meaningful within one continuous MCP
session — a real constraint on how AXIOM dispatches tool calls.

## Operations

```bash
# status
launchctl list | grep xrayai.rea
lsof -nP -iTCP:8787 -sTCP:LISTEN

# logs
tail -f ~/.local/share/rea/logs/mcp-server.log
tail -f ~/.local/share/rea/logs/tunnel.log

# restart after changing ~/.config/rea/env.sh
launchctl kickstart -k gui/$(id -u)/com.xrayai.rea.mcp

# uninstall
launchctl bootout gui/$(id -u)/com.xrayai.rea.mcp
launchctl bootout gui/$(id -u)/com.xrayai.rea.tunnel
```

VPS-side registration (already performed, id `mcp_09adc0a219be`):

```bash
curl -sk -X POST https://127.0.0.1:8888/api/v1/mcp/servers \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"name":"rea","url":"http://127.0.0.1:18787/mcp","transport":"http"}'
curl -sk -X POST .../api/v1/mcp/servers/mcp_09adc0a219be/discover -H "Authorization: Bearer $TOK"
```

## Still open

1. **Phase binding.** 229 tools are discovered but bound to no phase. Binding
   all of them would swamp the prompt; the candidate first set is
   `inspect_artifact`, `inspect_apk_signature`, `project_android_application_graph`,
   `inspect_android_package`, `analyze_javascript_application`,
   `inspect_electron_fuses`, `inspect_sqlite_database`, `inspect_plist`.
2. **Concurrency.** One HTTP session means serialized access. Running
   `ssh <mac>` as a *stdio* `command` instead of an HTTP `url` gives each worker
   its own REA process and its own active-target state.
3. **Privilege scope.** REA's file-reading tools run as `hexin` on the Mac.
   Anything that can speak to the tunnel can read anything that user can read.
   A dedicated macOS account for the server would bound that.
