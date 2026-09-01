<div align="center">

# AXIOM
### AI 驱动的渗透测试与通用问题求解引擎

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

> **基于 [oritera/Cairn](https://github.com/oritera/Cairn) 的 Fork** — 重构为纯本地部署，大幅改进前端，新增维护者阶段、资产数据库和架构清理。

---

## 概述

AXIOM 是一个通用问题求解引擎，基于**黑板架构**构建，使用显式的事实-意图图。给定一个起点和一个目标，它通过 LLM 驱动的 Agent Worker 在未知状态空间中搜索解决方案。

AI 渗透测试是第一个经过验证的领域。该引擎与领域无关——任何有明确起点、明确成功条件、中间路径未知的问题都适用同一模型。

### 核心概念

| 概念 | 含义 |
|---------|---------|
| **事实（Fact）** | 写入共享黑板的已确认的客观发现 |
| **意图（Intent）** | 已声明但尚未执行的探索方向 |
| **提示（Hint）** | 随时注入的人工判断；Worker 下次读取时吸收 |

图从 `origin` 向 `goal` 增长。Agent Worker 仅通过共享黑板协调（间接通信机制）——无直接通信，无信息孤岛。

---

## 系统架构

```
                         ┌──────────────┐
                         │   浏览器前端   │
                         └──────┬───────┘
                                │
                    ┌───────────┴───────────┐
                    │   AXIOM 服务端         │
                    │   (FastAPI + SQLite)   │
                    │   项目 · 意图 · 事实    │
                    │   提示 · 漏洞          │
                    └───────────┬───────────┘
                                │
                    ┌───────────┴───────────┐
                    │     调度器循环         │
                    │  每3秒 tick · 认领管理 │
                    │  Worker 选择           │
                    │  阶段限制              │
                    └───────────┬───────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        │                       │                       │
        ▼                       ▼                       ▼
  ┌──────────┐          ┌──────────┐          ┌──────────────┐
  │  推理阶段  │ ──意图→  │  探索阶段  │ ──发现报告→ │  维护者阶段   │
  │  Reason  │          │ Explore  │          │ Maintainer   │
  │qwen3.6+  │          │deepseek  │          │ stepfun-flash│
  │          │          │ / qwen   │          │              │
  └──────────┘          └──────────┘          └──────┬───────┘
        ▲                                           │
        │          新事实（触发下一轮推理）              │
        └───────────────────────────────────────────┘
                                                     │
                                              ┌──────┴───────┐
                                              │    资产数据库  │
                                              │   (SQLite)   │
                                              │ 接口 · 参数   │
                                              │ 测试状态      │
                                              └──────────────┘
```

### 三阶段流水线

调度器在三个阶段之间协调持续循环：

| 阶段 | Worker | 职责 | 产出 |
|-------|--------|------|--------|
| **Reason（推理）** | qwen3.6-plus | 读取完整事实图+资产库；判断目标是否达成或需要新意图 | 完成 / 新意图 / 无操作 |
| **Explore（探索）** | deepseekv4-pro / qwen3.6-plus | 认领一个意图，执行探索计划，运行安全工具 | 发现报告文件 + 新事实 |
| **Maintainer（维护者）** | stepfun-flash | 读取探索发现报告，提取 API 端点+请求体，上报漏洞 | 资产入库 · 漏洞上报 |

循环：**推理 → 意图 → 探索 → 发现报告 → 维护者 → 新事实 → 推理**（不断循环直到目标达成）。

### 推理触发逻辑

推理在以下情况触发：

- **初始触发** — 新项目首次调度
- **事实变化** — Explore Worker 产生了新事实
- **开放意图归零** — 所有活跃意图均已结项
- **过期兜底** — 超过 `reason.timeout` 秒无活动且有 0 个开放意图（防止永久空闲）

---

## 与上游的区别

### 维护者阶段与资产数据库
- **新增阶段**专门处理发现报告（stepfun-flash worker）
- SQLite 资产数据库（`/tmp/axiom-assets.db`）追踪所有已发现的 API 端点
- 维护者自动提取端点、请求体、Content-Type 和测试状态
- 通过唯一约束（项目、域名、路径、方法）自动去重
- 180s 超时 + 失败自动重试
- 支持多并发——多个维护者任务同时运行

### Worker 任务分配
- Worker 有明确的 `task_types`（bootstrap / reason / explore / plan / maintainer）
- Reason 阶段固定使用 qwen3.6-plus（deepseek 在大量事实的推理任务上不可靠）
- stepfun-flash 专用于维护者
- 阶段限制防止过载

### 前端重写
- 单文件 Alpine.js SPA 取代原有前端
- **资产管理页面** — 可筛选/搜索的 API 端点表格，鼠标悬停显示请求体详情
- **知识图谱可视化** 基于 Cytoscape.js + ELK 布局引擎
  - 卡片式节点，含 SVG 图标 + 类型标题 + 正文
  - 双击聚焦子图（上游+下游谱系）
  - 自绘 Canvas 小地图含视口指示器
  - 底部时间线滑动条带事件标记
- **设计系统**（`apple.css`）— Apple 风格 UI，组件一致
- 旧版前端保留在 `/legacy` 路由

### 过期推理处理
- 当所有意图完成但推理模型返回"无操作"时，调度器之前会永久空闲
- **已修复**：无活动且开放意图为 0 超过 `reason.timeout` 秒后，强制重新触发推理

### 架构变更
- 移除 Docker/容器层——完全通过 `start.sh` 本地运行
- 通过 `shared/` 包解耦服务端和调度器
- 清理死代码——Claude Code、Codex、Mock 驱动；容器管理器
- 纯 PI Worker——简化为单一 Worker 后端
- 深度模式——延迟推理直到本地探索完成
- Token 追踪——从 PI 会话文件端到端统计
- 黑板系统——结构化 Prompt 注入、懒加载、提示元数据

### 项目管理
- 项目分组，弹窗式创建和删除
- 全局搜索（Cmd+K）
- 深色模式切换
- 通知系统带错误详情弹窗

---

## 快速开始

### 环境要求

- macOS 或 Linux
- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/)（Python 包管理器）

### 安装

```bash
# 克隆仓库
git clone https://github.com/notstarr/AXIOM.git
cd AXIOM

# 创建调度器配置
cp dispatch.example.yaml dispatch.yaml
# 编辑 dispatch.yaml —— 填入你的 LLM 端点、模型和 API Key

# 安装依赖
cd cairn && uv sync && cd ..
```

### 运行

```bash
# 一键启动（服务端 + 调度器）
bash start.sh start

# 查看状态
bash start.sh status

# 停止
bash start.sh stop
```

启动后：
- **AXIOM Server** 监听 `8888` 端口
- **AXIOM Dispatcher**（服务端健康检查通过后自动启动）

访问 `http://localhost:8888` 打开 Web 界面。数据持久化到 `~/.local/share/cairn/cairn.db`。

### 手动启动

```bash
# 启动服务端
uv run --project cairn cairn serve

# 运行调度器（另一个终端）
uv run --project cairn cairn dispatch --config dispatch.yaml
```

---

## 项目结构

```
AXIOM/
├── cairn/                            # 主应用
│   ├── src/axiom/
│   │   ├── server/                   # FastAPI 服务端
│   │   │   ├── routers/              # API 路由（项目、意图、提示、资产、事实、漏洞……）
│   │   │   ├── static/               # 前端 SPA（index.html、apple.css、vendor/）
│   │   │   ├── app.py                # FastAPI 应用及路由注册
│   │   │   ├── db.py                 # SQLite 数据库层
│   │   │   └── models.py             # Pydantic 数据模型
│   │   ├── dispatcher/               # 任务调度引擎
│   │   │   ├── scheduler/            # 调度循环
│   │   │   ├── tasks/                # 任务执行器（Bootstrap、Reason、Explore、Plan、Maintainer）
│   │   │   ├── workers/              # PI Worker 适配器
│   │   │   ├── prompts/              # LLM Prompt 模板（default、depth）
│   │   │   ├── skills/               # Worker 技能（browser、nuclei、katana、whatisvul……）
│   │   │   └── tools/                # 网关、追踪器
│   │   └── shared/                   # 共享工具（密钥环、输出解析器）
│   └── pyproject.toml
├── tools/                            # 外部工具
│   ├── crawler/                      # Playwright CDP CLI 辅助工具
│   ├── mitm/                         # mitmproxy 流量录制插件
│   └── xss-verify/                   # 自动化 XSS 检测工具
├── docs/                             # 文档
├── dispatch.example.yaml             # 调度器配置模板（不含真实密钥）
├── start.sh                          # 一键启动/停止脚本
└── .gitignore
```

---

## 配置说明

### `dispatch.yaml`

```yaml
server: "http://localhost:8888"

runtime:
  interval: 3                  # 调度间隔（秒）
  max_workers: 8               # 最大并发 worker
  max_running_projects: 3      # 最大同时运行项目数
  max_project_workers: 4       # 每个项目最大 worker 数
  healthcheck_timeout: 60      # 启动健康检查超时（秒）
  worker_healthcheck: "startup_only"
  prompt_group: "default"

tasks:
  bootstrap: { timeout: 300, conclude_timeout: 90 }
  reason:    { timeout: 300, max_intents: 3, depth_max_intents: 5 }
  explore:   { timeout: 600, conclude_timeout: 150 }

workers:
  - name: "deepseek-worker"
    type: "pi"
    task_types: [bootstrap, explore, plan]    # 不包含 reason（深度推理不稳定）
    max_running: 2
    env:
      PI_MODEL: "your-model"
      PI_BASE_URL: "https://your-proxy/v1"
      PI_API_KEY: "sk-xxx"
      PI_PROVIDER_API: "openai-completions"

  - name: "qwen-worker"
    type: "pi"
    task_types: [bootstrap, reason, explore, plan]  # 负责推理任务
    max_running: 2
    env:
      PI_MODEL: "your-model"
      PI_BASE_URL: "https://your-proxy/v1"
      PI_API_KEY: "sk-xxx"
      PI_PROVIDER_API: "openai-completions"

  - name: "maintainer-worker"
    type: "pi"
    task_types: [maintainer]                     # 专用维护者
    max_running: 2
    env:
      PI_MODEL: "your-model"
      PI_BASE_URL: "https://your-proxy/v1"
      PI_API_KEY: "sk-xxx"
      PI_PROVIDER_API: "openai-completions"
```

API Key 使用 `MASTER_KEY` 加密存储（首次运行时自动生成，保存在 `~/.config/cairn/master.key`）。

---

## 免责声明

AXIOM 是一个通用问题求解引擎。虽然它支持渗透测试、CTF 解题、安全评估和漏洞研究工作流，但它仅应在**你拥有明确操作授权**的环境中使用。

你对自己如何使用本项目负有完全责任。未经所有者或运营方明确的事先许可，不得对系统、网络、应用程序或数据使用 AXIOM。

本项目的开发者和贡献者不对任何滥用、损害、损失或法律后果承担或认可任何责任。

---

## 致谢

- **[oritera/Cairn](https://github.com/oritera/Cairn)** — 本项目基于的原始项目
- **[Cytoscape.js](https://js.cytoscape.org/)** — 图可视化库
- **[ELK](https://www.eclipse.org/elk/)** — 布局引擎
- **[FastAPI](https://fastapi.tiangolo.com/)** — Web 框架
- **[Alpine.js](https://alpinejs.dev/)** — 前端响应式框架

---

## 许可证

本项目采用 **GNU AGPLv3** 许可证，适用于个人和教育用途。

**商业用途**：如果你希望在商业或专有环境中使用本项目而不受 AGPL-3.0 开源义务的约束，请联系维护者获取商业许可证。
