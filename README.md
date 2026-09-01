<div align="center">

# AXIOM
### 多智能体协同的自动化渗透测试编排平台

<p>
  <a href="https://github.com/notstarr/AXIOM" target="_blank" rel="noopener noreferrer">
    <img src="https://img.shields.io/badge/GitHub-notstarr/AXIOM-181717?style=flat-square&logo=github" alt="GitHub" />
  </a>
  <a href="./LICENSE" target="_blank" rel="noopener noreferrer">
    <img src="https://img.shields.io/badge/License-AGPL--3.0-blue?style=flat-square" alt="License" />
  </a>
  <img src="https://img.shields.io/badge/Python-3.12+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/PostgreSQL-18-4169E1?style=flat-square&logo=postgresql&logoColor=white" alt="PostgreSQL" />
  <img src="https://img.shields.io/badge/tests-734%20passed-2EA043?style=flat-square" alt="Tests" />
</p>

</div>

---

AXIOM 以 PostgreSQL 为唯一事实源，通过五阶段确定性状态机驱动 Worker 池执行侦察、推理、实操、规划与复核，产出经过独立复现验证的真实漏洞报告。系统采用模块化单体设计，内置 Web 运营控制台、原生浏览器沙箱、可插拔技能库、命令守门审计与钉钉双向机器人，面向**授权范围内**的安全评估场景。

> 本仓库 `axiom/` 目录为当前唯一活跃的 V2 平台（PostgreSQL 原生架构）；根目录 `cairn/` 为旧版 SQLite 架构的历史代码，已停止维护，仅作存档。

## 系统架构

两个宿主机常驻进程 + 一个 PostgreSQL 实例，API 是唯一的通信总线：

```text
┌──────────────────────────────────────────────────────────────┐
│                         PostgreSQL 18                        │
│          127.0.0.1:55432 · DB: axiom · Schema: axiom         │
└───────────────▲──────────────────────────────────────────────┘
                │ (SQLAlchemy / Alembic)
┌───────────────┴──────────────────────────────────────────────┐
│  axiom serve  (FastAPI + Uvicorn, 默认端口 8888)              │
│  · RESTful API / WebSocket（/api/v1，33 个领域路由）          │
│  · Web 运营控制台与静态资源托管                               │
│  · 登录鉴权 · 钉钉 Webhook · 模型网关代理                     │
└───────────────▲──────────────────────────────────────────────┘
                │ (HTTP API, API-only 无直连 DB)
┌───────────────┴──────────────────────────────────────────────┐
│  axiom runner  (调度器 + 心跳)                                │
│  · Worker 领取 Lease、阶段执行与超时管控                      │
│  · 子进程 Pi Agent 启动与会话跟踪                             │
│  · Browser Broker 原生浏览器沙箱管理                          │
└──────────────────────────────────────────────────────────────┘
```

## 五阶段流水线

```text
[ 新建任务 (Task) ]
        │
        ▼
 1. Bootstrap（初始测绘）—— JS Chunk / SPA 路由 / Source Map / API 能力树 / 鉴权矩阵
        │
        ▼
 2. Reason（增量推理规划）—— 动态 Todo 树与 Intent 派发，单账号自建自测闭环
        │
        ▼
 3. Explore（动态实操与验证）—— Browser Broker 深层交互，凭据自动提取，结构化证据
        │
        ▼
 4. Plan（增量漏洞规划）—— 基于已确认事实规划下一轮攻击面
        │
        ▼
 5. Review（独立交叉复现审计）—— 端到端复现，剔除误报，给出真实裁决与理由
```

**漏洞判定原则：危害驱动（Impact-Driven）。** 没有真实危害证据的空壳发现会被 Review 阶段直接剔除。

## Web 运营控制台

单页控制台（Alpine.js + Tailwind，统一 sto-* 设计系统，亮/暗双主题），18 个页面：仪表盘、任务管理（卡片自带生命体征点与统计）、图谱视图、漏洞管理（严重度色彩体系 + 漏洞包导出）、项目管理、守门审计、Worker 管理（分阶段能力与模型后端）、工具管理、Skill 管理、产物中心（Markdown 渲染 / 代码高亮预览）、MCP 管理、提示词、任务归档、资产测绘、爬虫、人工视图、动作回放、设置（含存储治理三档清理）。

## 核心子系统

| 子系统 | 说明 |
|---|---|
| Worker 池与模型后端 | 按阶段（bootstrap/reason/explore/plan/review）分配能力；Pi 兼容网关模型发现、上下文窗口、思考强度；Provider 错误分类与冷却 |
| Browser Broker | 受控 Chrome 沙箱（与日常浏览器物理隔离）、CDP、抓包、`browser_get_credentials` 凭据提取 |
| 内置技能库（16 项） | nmap / subfinder / httpx / katana / ffuf / nuclei / sqlmap / whatisvul / vuln-report / vuln-review / xss-verify / business-logic-testing / browser-control / credential-refresh / miniapp / agent-mail |
| 守门审计（Guard） | Worker 敏感命令策略裁决，审计全量落库，控制台按裁决着色 |
| 漏洞管理与评审 | 上报 → 独立复现 → 定论状态机；严重度体系；Markdown 报告 + 证据导出 |
| MCP 管理 | Server 发现与导入、工具浏览与启用控制 |
| 存储治理 | 三档清理中心（一键 / 预览确认 / 锁定展示）、日志轮转 |
| Token 用量 | 按 Intent / 模型 / 阶段统计与缓存命中率，烧钱热点排行 |
| 钉钉机器人 | 两阶段精准推送（初报 → 评审定论）；群内 `/tasks` `/status` `/pause` `/resume` `/vulns` `/vuln` 双向指令 |
| 人工干预与可观测 | Explore 实时对话与人工插话、动作回放、诊断中心、PostgreSQL 备份 / 快照 / 恢复演练 |

## 快速部署

环境要求：macOS（Apple Silicon 推荐）或 Linux、Python 3.12+（uv）、Docker。

```bash
cd axiom

# 1. 启动 PostgreSQL（含自动建库与最小权限角色初始化）
docker compose -f compose.yaml -f compose.host.yaml up -d postgres

# 2. 同步依赖
uv sync --frozen

# 3. 数据库迁移
.venv/bin/axiom db upgrade

# 4. 启动 API + Web 控制台
.venv/bin/axiom serve --host 127.0.0.1 --port 8888 --data-dir ~/.local/share/axiom-v3 &

# 5. 启动调度 Runner
.venv/bin/axiom runner --api-url http://127.0.0.1:8888 &
```

访问 `http://127.0.0.1:8888` 进入控制台。首次启动自动创建管理员（用户名取 `AXIOM_ADMIN_USER`，默认 `axiom`；密码取 `AXIOM_ADMIN_PASSWORD`，未设置则随机生成并写入数据目录）。生产环境建议以 launchd / systemd 托管进程。

## 一键升级

```bash
cd axiom && ./scripts/upgrade.sh
```

安全机制：数据库连通性检查 → 拉取最新代码 → 依赖同步 → 迁移（失败自动回滚）→ 平滑重启 → 健康检查确认。

## 仓库结构

```text
axiom/                        # ★ 当前 V2 平台（唯一活跃开发目录）
├── compose.yaml              # PostgreSQL 容器编排（镜像按 digest 固定）
├── pyproject.toml / uv.lock  # 依赖（FastAPI / SQLAlchemy / Playwright / MCP 等）
├── migrations/               # Alembic 数据库迁移
├── scripts/upgrade.sh        # 一键升级
├── ops/postgres/             # 备份、快照、恢复演练、观察者
├── docs/                     # 按日期归档的设计与变更文档
└── src/axiom/
    ├── api/                  # 路由汇聚（33 个领域路由）
    ├── dispatcher/           # 调度引擎：五阶段执行器、browser_broker、prompts/、skills/
    ├── modules/              # 33 个领域模块（tasks/goals/reason/plan/graph/workers/
    │                         #   vulnerabilities/vulnerability_reviews/guard/skills/mcp/
    │                         #   storage_admin/token_usage/interventions/auth/...）
    ├── shared/               # 共享底座（database、dingtalk、storage、ids）
    └── static/               # Web 控制台（18 页 + sto-* 设计系统）
cairn/                        # 旧版 SQLite 架构（已停维护，仅存档）
README/                       # README 图片资源
```

## 测试

```bash
cd axiom && .venv/bin/python3 -m pytest -q    # 734 个用例
```

## 安全与合规声明

AXIOM 仅用于**获得明确书面授权**的安全评估场景。使用者须遵守当地法律法规，对目标系统具备合法测试授权；因滥用本工具造成的任何后果由使用者自行承担。
