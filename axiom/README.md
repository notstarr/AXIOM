# AXIOM

AXIOM 是一个基于多智能体协同、具备确定性状态机与动态沙箱的自动化渗透测试与安全评估系统。

系统基于 **PostgreSQL 原生架构** 驱动，采用 **模块化单体（Modular Monolith）** 设计，将资产测绘、API 逆向分析、漏洞增量规划、浏览器交互验证与独立复现审计组织为严密的自动化流水线。

---

## 1. 系统架构与核心进程

AXIOM 由两个相互独立的宿主机长期运行进程组成，以 API 和 PostgreSQL 为唯一事实源：

```text
┌─────────────────────────────────────────────────────────────┐
│                         PostgreSQL                          │
│     127.0.0.1:55432 / DB: axiom / Schema: axiom             │
└──────────────▲───────────────────────────────▲──────────────┘
               │                               │ (Alembic/服务连接)
┌──────────────┴───────────────────────────────┴──────────────┐
│  axiom serve                                                │
│  - RESTful API / WebSocket 接口 (/api/v1)                   │
│  - 业务领域模型 (Tasks, Graphs, Todos, Vulnerabilities)     │
│  - 钉钉告警通知与 Webhook 回调路由                          │
│  - 前端 Web 控制台与静态资源托管 (端口 8888)                │
└──────────────────────────────▲──────────────────────────────┘
                               │ (HTTP API 访问)
┌──────────────────────────────┴──────────────────────────────┐
│  axiom runner                                               │
│  - API-only 调度器与心跳维护                                 │
│  - Worker 领取、阶段执行与超时管控                          │
│  - Browser Broker 原生浏览器沙箱管理                        │
│  - 子进程 Pi Agent 启动与会话跟踪                           │
└─────────────────────────────────────────────────────────────┘
```

- **`axiom serve`**：提供控制面与数据面 API，负责业务事务落库、权限鉴权与前端交互；
- **`axiom runner`**：不直接连接数据库，仅通过 `/api/v1/dispatch/*` 接口与 API 通信，负责领取执行 Lease 并驱动各阶段智能体。

---

## 2. 多智能体协作流水线

任务生命周期遵循确定的状态流转机制：

```text
[ 新建任务 (Task) ]
        │
        ▼
 1. Bootstrap (初始测绘)
    ├── 收集静态/动态 JS Chunk、SPA 路由与 Source Map
    ├── 识别 API 前缀域并提取底座拼装函数
    └── 展开完整 API 能力树，标定初始鉴权矩阵与基线对象 ID
        │
        ▼
 2. Reason (增量推理规划)
    ├── 维护动态 Todo 树与 Intent 派发
    ├── 落实单账号「自建自测闭环」与单向攻击面，严禁偷懒式 Blocked
    └── 关联新 Fact 事实，评估阶段 Goal 达成状态
        │
        ▼
 3. Explore (动态实操与验证)
    ├── 调度 Browser Broker 原生浏览器沙箱进行深层交互
    ├── 自动提取 Cookie 与 JWT 凭据 (`browser_get_credentials`)
    └── 输出结构化证据、复现步骤与待验证漏洞报告
        │
        ▼
 4. Review (独立交叉复现与审计)
    ├── 针对上报的漏洞进行独立端到端复现
    └── 剔除虚假报错与误报，给出真实裁决理由（已验证 / 忽略 / 重复）
```

---

## 3. 核心子系统

### 3.1 Browser Broker (原生浏览器沙箱)
- 独立调度受控浏览器实例（数据目录 `~/Library/Application Support/Chrome-SecTest`），与日常个人 Chrome 物理隔离；
- 支持 CDP 调试连接、页面快照解析、DOM 节点交互、网络抓包监控；
- **凭据提取工具 (`browser_get_credentials`)**：按目标域名自动提取 Cookie 及 LocalStorage/SessionStorage 中的 Token，自动格式化为标准 HTTP 请求头。

### 3.2 钉钉机器人与告警通知
- **两阶段精准推送闭环**：
  - *发现初报*：探针上报待验证漏洞时，立即推送包含复现 Curl、受影响资产与描述的告警卡片；
  - *评审定论*：Reviewer 裁决完毕后，立即推送包含最终状态与 **真实评审理由 (`status_reason`)** 的卡片。
- **群内双向指令控制**：
  - `/help`：获取分类指令指南；
  - `/tasks`：查看正在运行的任务清单；
  - `/status <任务ID>`：查询任务进度与当前阶段（支持纯数字简写，如 `/status 172`）；
  - `/pause <任务ID>`：暂停指定任务；
  - `/resume <任务ID>`：继续/恢复任务（**默认不限时持续运行**）；
  - `/vulns <任务ID>`：查看该任务已发现的漏洞成果清单；
  - `/vuln <漏洞ID>`：查看单条漏洞的复现 Curl 与详细分析。

---

## 4. 快速部署与运行

### 4.1 环境要求
- **系统**：macOS (Apple Silicon ARM64 推荐) 或 Linux
- **Python**：Python 3.12+ (推荐使用 `uv` 包管理器)
- **数据库**：PostgreSQL 18.4 (推荐 Docker 部署，默认端口 `55432`)

### 4.2 安装与启动

```bash
# 1. 克隆仓库
git clone https://github.com/notstarr/AXIOM.git
cd AXIOM

# 2. 启动 PostgreSQL 数据库容器
docker compose -f compose.yaml -f compose.host.yaml up -d postgres

# 3. 同步 Python 虚拟环境与依赖
uv sync --frozen

# 4. 执行数据库 Schema 迁移
.venv/bin/axiom db upgrade

# 5. 启动服务
# 启动 API 与 Web 服务
.venv/bin/axiom serve --host 127.0.0.1 --port 8888 --data-dir ~/.local/share/axiom-v3 &

# 启动任务调度 Runner
.venv/bin/axiom runner --api-url http://127.0.0.1:8888 &
```

访问 `http://127.0.0.1:8888` 即可进入 Web 控制台。

---

## 5. 一键平滑升级与自愈

系统内置了自动化升级脚本 `scripts/upgrade.sh`：

```bash
# 本地一键升级
./scripts/upgrade.sh

# 强制暂存本地修改并升级
./scripts/upgrade.sh --force

# 远程管道一键升级
curl -fsSL https://raw.githubusercontent.com/notstarr/AXIOM/main/scripts/upgrade.sh | bash
```

**升级安全机制：**
1. 检查 PostgreSQL 与 Docker 容器连接；
2. 拉取远端最新 `main` 分支代码；
3. 同步 Python 依赖 (`uv sync`)；
4. 执行 `axiom db upgrade` 迁移数据库（失败自动 `git reset` 回滚）；
5. 平滑重启后台守护进程（macOS `launchd` / Linux `systemd`）；
6. 轮询 API 进行健康检查确认升级成功。

---

## 6. 常用 CLI 命令

```bash
# 查看全局命令帮助
axiom --help

# 数据库迁移与操作
axiom db upgrade

# 钉钉连通性测试
axiom dingtalk test

# 查看 Token 消耗统计
axiom token-usage

# 技能状态与排障
axiom skill list
axiom skill doctor whatisvul
```

---

## 7. 仓库目录结构

```text
axiom/
├── compose.yaml                # PostgreSQL 容器定义
├── compose.host.yaml           # 宿主机端口映射 (55432)
├── pyproject.toml              # 依赖与打包配置
├── scripts/
│   └── upgrade.sh              # 一键自动化升级脚本
├── src/axiom/
│   ├── api/                    # 路由汇聚与控制面入口
│   ├── cli.py                  # CLI 命令注册入口
│   ├── dispatcher/             # 调度引擎、执行器与沙箱
│   │   ├── browser_broker.py   # Browser Broker 原生浏览器沙箱
│   │   ├── prompts/            # Bootstrap, Reason, Explore, Review 提示词
│   │   └── skills/             # 内置安全技能库 (whatisvul, ffuf, xss-verify 等)
│   ├── modules/                # 领域业务模块
│   │   ├── auth/               # 认证与 Session 管理
│   │   ├── integrations/       # 钉钉 Webhook 与交互指令 API
│   │   ├── settings/           # 运行时配置与持久化
│   │   ├── tasks/              # 任务生命周期与状态机
│   │   ├── todos/              # 动态 Todo 管理
│   │   ├── vulnerabilities/    # 漏洞模型与上报
│   │   └── vulnerability_reviews/ # 独立复现与评审
│   ├── shared/                 # 共享底座 (database, dingtalk, storage, ids)
│   └── static/                 # Web 前端控制台静态资源
└── tests/                      # 测试用例集
```
