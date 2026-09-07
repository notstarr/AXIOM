<div align="center">

# AXIOM
### AI 驱动的自动化渗透测试编排平台

<p>
  <a href="https://github.com/notstarr/AXIOM" target="_blank" rel="noopener noreferrer">
    <img src="https://img.shields.io/badge/GitHub-notstarr/AXIOM-181717?style=flat-square&logo=github" alt="GitHub" />
  </a>
  <a href="./LICENSE" target="_blank" rel="noopener noreferrer">
    <img src="https://img.shields.io/badge/License-AGPL--3.0-blue?style=flat-square" alt="License" />
  </a>
  <img src="https://img.shields.io/badge/Python-3.12+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/FastAPI-latest-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/PostgreSQL-18.4-4169E1?style=flat-square&logo=postgresql&logoColor=white" alt="PostgreSQL" />
</p>

</div>

> 历史说明：本项目起源于 [oritera/Cairn](https://github.com/oritera/Cairn) 的 Fork。Cairn 时代的 SQLite 单库、三阶段（Reason/Explore/Maintainer）流水线与 `cairn/` 目录结构**已被完全替换**，不复存在——当前架构以 [`axiom/README.md`](./axiom/README.md) 为准。

---

## 概述

AXIOM 是一个多智能体协同的自动化渗透测试编排平台：以 **PostgreSQL 为唯一事实源**，通过**五阶段确定性状态机**（Bootstrap → Reason → Explore → Plan → Review）驱动 Worker 池执行侦察、推理、实操、规划与独立复核，产出经过独立复现验证的真实漏洞报告。

- 模块化单体：FastAPI 服务端 + API-only 调度 Runner + PostgreSQL，两进程一库；
- 执行面为 Pi 编码智能体子进程，配原生浏览器沙箱（Playwright + 受控 Chrome）、任务级执行沙箱、16 项内置安全技能；
- 命令守门审计、漏洞独立评审状态机、钉钉双向机器人、Web 运营控制台。

详细的系统架构、五阶段流水线、控制台页面与部署方式见 **[`axiom/README.md`](./axiom/README.md)**。

---

## 快速开始

```bash
# 1. 启动 PostgreSQL
docker compose -f axiom/compose.yaml -f axiom/compose.host.yaml up -d postgres

# 2. 同步依赖并迁移
cd axiom && uv sync --frozen && .venv/bin/axiom db upgrade

# 3. 启动 API（默认 127.0.0.1:8899）与调度 Runner
.venv/bin/axiom serve --data-dir ~/.local/share/axiom-v3 &
.venv/bin/axiom runner --api-url http://127.0.0.1:8899 &
```

或使用仓库根的 `./start.sh`（launchd 托管 serve/runner + PostgreSQL compose）。

一键 Docker 部署（PostgreSQL + API + Runner 三件套，宿主机端口默认 `18888`）：

```bash
docker compose -f axiom/compose.deploy.yaml up -d --build
```

---

## 仓库结构

```text
AXIOM/
├── axiom/            # 主项目：渗透测试编排平台（架构与部署文档见 axiom/README.md）
│   ├── src/axiom/    # 服务端、领域模块、调度器、Web 控制台
│   ├── migrations/   # PostgreSQL Alembic 迁移链
│   ├── ops/          # PostgreSQL 备份 / 恢复演练 / 观察者
│   └── tests/        # 763 个测试用例
├── start.sh          # 一键启动 / 停止 / 状态（launchd + compose）
├── docs/             # 历史设计文档（部分为 Cairn 时代遗留，以 axiom/README.md 为准）
└── LICENSE           # AGPL-3.0
```

> 仓库根目录其余 `*.md`（handoff、SKILL_GUIDE、MODE_COMPARISON 等）为历史开发/交接文档，内容可能已过时，**不作为产品说明的依据**。

---

## 免责声明

AXIOM 仅用于**获得明确书面授权**的安全评估场景。未经所有者或运营方明确的事先许可，不得对系统、网络、应用程序或数据使用 AXIOM。使用者对自己如何使用本项目负有完全责任；开发者和贡献者不对任何滥用、损害、损失或法律后果承担责任。

## 致谢

- **[oritera/Cairn](https://github.com/oritera/Cairn)** — 项目起源
- **[FastAPI](https://fastapi.tiangolo.com/)** / **[Alpine.js](https://alpinejs.dev/)** / **[Cytoscape.js](https://js.cytoscape.org/)** / **[ELK](https://www.eclipse.org/elk/)** — 关键依赖

## 许可证

本项目采用 **GNU AGPLv3** 许可证。**商业用途**：如需在商业或专有环境中使用本项目而不受 AGPL-3.0 开源义务约束，请联系维护者获取商业许可证。
