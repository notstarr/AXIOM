<div align="center">

<img src="./README/banner.png" alt="AXIOM Banner"/>

# AXIOM
### AI-Driven Penetration Testing & General Problem-Solving Engine

<p>
  <a href="https://github.com/notstarr/AXIOM" target="_blank" rel="noopener noreferrer">
    <img src="https://img.shields.io/badge/GitHub-notstarr/AXIOM-181717?style=flat-square&logo=github" alt="GitHub" />
  </a>
  <a href="./LICENSE" target="_blank" rel="noopener noreferrer">
    <img src="https://img.shields.io/badge/License-AGPL--3.0-blue?style=flat-square" alt="License" />
  </a>
  <img src="https://img.shields.io/badge/Python-3.12+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/FastAPI-latest-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI" />
</p>

</div>

> **Fork of [oritera/Cairn](https://github.com/oritera/Cairn)** — restructured for local-only deployment with a major frontend overhaul, crawler system, and architecture cleanup.

---

## Overview

AXIOM is a general-purpose problem-solving engine built on a **Blackboard Architecture** with an explicit fact-intent graph. Given an origin and a goal, it searches through an unknown state space using LLM-powered agent workers.

AI Penetration Testing is the first validated domain. The engine is domain-agnostic — any problem with a clear starting point, a clear success condition, and an unknown path in between fits the same model.

### Core Primitives

| Concept | Meaning |
|---------|---------|
| **Fact** | A confirmed, objective finding written to the shared board |
| **Intent** | A declared direction of exploration, not yet executed |
| **Hint** | Human judgment injected at any time; absorbed by agents on the next read |

The graph grows from `origin` toward `goal`. Agent Workers run an OODA loop (Observe → Orient → Decide → Act) and coordinate exclusively through the shared board (Stigmergy) — no direct communication, no information silos.

---

## What's Different from Upstream

This fork removes Docker/container dependencies entirely and runs locally with significant new features:

### Frontend Overhaul
- **Single-file Alpine.js SPA** (~7800 lines) replacing the original multi-page UI
- **Knowledge graph visualization** using Cytoscape.js + ELK layout engine
  - Card-style nodes with SVG icons + type headers + body text (via `cytoscape-html-label`)
  - White background, light card design (not dark-themed like upstream)
  - Double-tap to focus subgraph (upstream + downstream lineage)
  - Self-drawn canvas minimap with viewport indicator
  - Bottom timeline scrubber with event markers
  - Curved bezier edges with status-colored arrows
- **Design system** (`apple.css`, ~1770 lines) — Apple-inspired UI with consistent components
- Legacy frontend preserved at `/legacy` route for reference

### Crawler System
- **Playwright CDP integration** — controls Chrome via DevTools Protocol for authenticated crawling
- **mitmproxy addon** — records HTTP traffic filtered by target domain
- **Action-oriented crawler prompts** — dedup, business-tree view, phased execute/flush loops
- **Crawler results page** with snapshot preview
- Refactored from monolithic `crawler.py` into modular `crawler/` package (8 modules)

### Architecture Changes
- **Removed Docker/container layer** — runs entirely locally via `start.sh`
- **Decoupled server from dispatcher** via `shared/` package (output_parser, keyring)
- **Cleaned dead code** — removed Claude Code, Codex, Mock drivers; Klay layout; container manager
- **PI worker only** — streamlined to a single worker backend
- **Depth mode** — straggler-tolerant reason triggering, deferred until local explores complete
- **Token tracking** — end-to-end from PI session files
- **Blackboard system** — structured prompt injection, lazy loading, hint metadata

### Project Management
- Project groups with modal-based creation and deletion
- Tree view for business scope navigation
- Global search (Cmd+K)
- Dark mode toggle
- Notification system with error detail popups

---

## System Architecture

```
          ┌──────────────────────────────────┐
          │           AXIOM Server           │
          │    Facts + Intents + Hints       │
          │    (FastAPI + SQLite)            │
          └─────────────────┬────────────────┘
                            │
                     Read / Write API
                            │
          ┌─────────────────┴────────────────┐
          │             Dispatcher           │
          │   Schedules tasks, manages       │
          │   workers, writes protocol       │
          └──────────┬───────────────────────┘
                     │
      ┌──────────────┴──────────────┐
      │       PI Worker (Local)      │
      │   Bootstrap / Reason / Explore│
      │   ┌─────┐  ┌─────┐  ┌─────┐  │
      │   │ W.  │  │ W.  │  │ W.  │  │
      │   └─────┘  └─────┘  └─────┘  │
      └──────────────────────────────┘
```

### Task Types

| Task | What it does | Output |
|------|-------------|--------|
| **Bootstrap** | At project start, attempts to solve the problem directly | Fact + possible Complete |
| **Reason** | Reads the full graph: is the goal met? What should be explored next? | Complete / new Intents / no-op |
| **Explore** | Claims one Intent, executes the exploration, reports findings | One Fact |

---

## Getting Started

### Prerequisites

- macOS or Linux
- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/) (Python package manager)

### Setup

```bash
# Clone
git clone https://github.com/notstarr/AXIOM.git
cd AXIOM

# Create dispatcher config
cp dispatch.example.yaml dispatch.yaml
# Edit dispatch.yaml — fill in your LLM endpoint, model, and API key

# Install dependencies
cd cairn && uv sync && cd ..
```

### Run

```bash
# One-command start (server + dispatcher)
bash start.sh start

# Check status
bash start.sh status

# Stop
bash start.sh stop
```

This starts:
- **AXIOM Server** on port `8888`
- **AXIOM Dispatcher** (auto-starts once server passes health check)

The server serves the web UI at `http://localhost:8888`. Data persists to `~/.local/share/cairn/cairn.db`.

### Manual Start

```bash
# Start the server
uv run --project cairn cairn serve

# Run the dispatcher (separate terminal)
uv run --project cairn cairn dispatch --config dispatch.yaml
```

### Tests

```bash
cd cairn
uv run pytest tests/ -q
```

---

## Project Structure

```
AXIOM/
├── cairn/                          # Main application
│   ├── src/axiom/
│   │   ├── server/                 # FastAPI server
│   │   │   ├── routers/            # API routes (projects, intents, hints, crawler, ...)
│   │   │   ├── static/             # Frontend SPA (index.html, apple.css, vendor/)
│   │   │   ├── app.py              # FastAPI app + routes
│   │   │   ├── db.py               # SQLite database layer
│   │   │   └── models.py           # Pydantic models
│   │   ├── dispatcher/             # Task scheduling engine
│   │   │   ├── scheduler/          # Scheduling loop
│   │   │   ├── tasks/              # Bootstrap, Reason, Explore task runners
│   │   │   ├── workers/            # PI worker adapter
│   │   │   ├── prompts/            # LLM prompt templates (default, depth, other)
│   │   │   └── tools/              # Gateway, tracker
│   │   └── shared/                 # Shared utilities (keyring, output_parser)
│   └── pyproject.toml
├── tools/                          # External tooling
│   ├── crawler/                    # Playwright CDP CLI helper
│   ├── mitm/                       # mitmproxy traffic recording addon
│   └── xss-verify/                 # XSS detection tool
├── docs/                           # Documentation
├── dispatch.example.yaml           # Example dispatcher config
├── start.sh                        # One-command start/stop script
└── .gitignore
```

---

## Crawler Tools (Optional)

AXIOM includes a crawler system for authenticated web application testing:

- **CDP Helper** (`tools/crawler/cdp_ctl.py`) — Playwright-based Chrome DevTools Protocol CLI
- **MITM Proxy** (`tools/mitm/mitm_addon.py`) — Records HTTP traffic to JSONL, filtered by target domain
- **XSS Verify** (`tools/xss-verify/`) — Automated XSS detection with headless browser

These integrate with the crawler router (`cairn/src/axiom/server/routers/crawler/`) for phased execute/flush workflows.

---

## Configuration

### `dispatch.yaml`

```yaml
server: "http://localhost:8888"

runtime:
  interval: 3
  max_workers: 8
  max_running_projects: 3
  max_project_workers: 4
  worker_healthcheck: "startup_only"
  prompt_group: "default"

tasks:
  bootstrap: { timeout: 300, conclude_timeout: 90 }
  reason:    { timeout: 300, max_intents: 2 }
  explore:   { timeout: 300, conclude_timeout: 90 }

workers:
  - name: "pi_worker_1"
    type: "pi"
    task_types: [bootstrap, reason, explore]
    max_running: 2
    env:
      PI_MODEL: "your-model-name"
      PI_BASE_URL: "https://api.your-provider.com/v1"
      PI_API_KEY: "sk-xxx"
      PI_PROVIDER_API: "openai-completions"
      PI_MODEL_CONTEXT_WINDOW: "128000"
```

API keys are encrypted at rest using a `MASTER_KEY` (auto-generated on first run, stored at `~/.config/cairn/master.key`).

---

## Disclaimer

AXIOM is a general-purpose problem-solving engine. Although it supports penetration testing, CTF solving, security assessment, and vulnerability research workflows, it is intended to be used **only in environments where you have explicit authorization to operate**.

You are solely responsible for how you use this project. Do not use AXIOM against systems, networks, applications, or data without clear prior permission from the owner or operator.

The developers and contributors of this project do not endorse or accept responsibility for any misuse, abuse, damage, loss, or legal consequences arising from its use.

---

## Acknowledgments

- **[oritera/Cairn](https://github.com/oritera/Cairn)** — Original project this fork is based on
- **[Cytoscape.js](https://js.cytoscape.org/)** — Graph visualization
- **[ELK](https://www.eclipse.org/elk/)** — Layout engine
- **[FastAPI](https://fastapi.tiangolo.com/)** — Web framework
- **[Alpine.js](https://alpinejs.dev/)** — Frontend reactivity

---

## License

This project is licensed under **GNU AGPLv3** for personal and educational use.

**Commercial Use**: If you wish to use this project in a commercial or proprietary environment without the AGPL-3.0 open-source obligations, please contact the maintainer to obtain a commercial license.
