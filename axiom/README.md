# AXIOM

AXIOM 是一个多智能体协同的自动化渗透测试编排平台：以 PostgreSQL 为唯一事实源，通过五阶段确定性状态机驱动 Worker 池执行侦察、推理、实操、规划与复核，产出经过独立复现验证的真实漏洞报告。

系统采用**模块化单体（Modular Monolith）**设计，内置 Web 运营控制台、原生浏览器沙箱、任务级执行沙箱、可插拔技能库、命令守门审计与钉钉双向机器人，面向**授权范围内**的安全评估场景。

---

---

## 0. 快速部署（新设备，从 GitHub）

两条路线，按需选择：

### 路线 A：容器化一键部署（推荐，需要 Docker）

**最快方式：拉 GHCR 预构建镜像（GitHub Actions 每次 push 自动构建发布），不用本地构建：**

```bash
git clone -b feat/reason-queue-050a20a https://github.com/notstarr/AXIOM.git && cd AXIOM
make pull-up        # = 拉镜像 + 起三件套 + 健康检查；就绪后访问 http://127.0.0.1:18888
make upgrade        # 日常升级：git pull + 换最新镜像（秒级，数据卷不受影响）
```

**或本地构建：**

```bash
git clone -b feat/reason-queue-050a20a https://github.com/notstarr/AXIOM.git && cd AXIOM
make up             # = docker compose -f compose.deploy.yaml up -d --build
```

三件套（PostgreSQL + API + Runner）全自动：建库、迁移、首次管理员由 `AXIOM_ADMIN_USER/AXIOM_ADMIN_PASSWORD` 环境变量播种。Pi Worker 运行时在镜像构建阶段按 `pi_config.py` 的版本常量自动从 npm 拉取——干净 clone 也能构建。升级语义与云上应用一致：**镜像不可变替换，数据卷持久**（`make upgrade` 即可）。

### 路线 B：宿主机原生部署（macOS launchd / Linux nohup）

```bash
git clone -b feat/reason-queue-050a20a https://github.com/notstarr/AXIOM.git && cd AXIOM

# 1. Python 依赖（uv 或 venv）
uv sync --frozen          # 或: python3 -m venv .venv && .venv/bin/pip install -e .

# 2. Pi Worker 运行时（按 pi_config.py 常量自动下载 node + npm 包）
./deploy/install-pi-runtime.sh

# 3. Worker 安全工具链（nmap/subfinder/httpx/nuclei/katana/ffuf/naabu/sqlmap/fscan，
#    版本基准见 deploy/tools-versions.env，走 GitHub API 资产流下载）
./deploy/install-worker-tools.sh

# 4. 首次管理员（启动前设置，启动时自动播种）
export AXIOM_ADMIN_USER=axiom
export AXIOM_ADMIN_PASSWORD='你的强密码'

# 5. 启动（自动拉起 PostgreSQL 容器、跑迁移、生成 launchd plist / nohup 进程）
./deploy/start.sh          # macOS: launchd 托管；Linux: nohup + pid 文件
./deploy/restart.sh        # 日常重启 API + Runner
```

版本对齐基准见 `deploy/tools-versions.env` 与 `src/axiom/modules/workers/pi_config.py`（`PI_RUNTIME_VERSION` 是 Pi 运行时的唯一真相源）。健康检查：`curl http://127.0.0.1:8888/health`。

---

## 1. 系统架构

AXIOM 由两个宿主机常驻进程与一个 PostgreSQL 实例组成，API 是唯一的通信总线：

```text
┌──────────────────────────────────────────────────────────────┐
│                        PostgreSQL 18.4                       │
│          127.0.0.1:55432 · DB: axiom · Schema: axiom         │
└───────────────▲──────────────────────────────────────────────┘
                │ (SQLAlchemy / Alembic)
┌───────────────┴──────────────────────────────────────────────┐
│  axiom serve  (FastAPI + Uvicorn, 默认端口 8899)              │
│  · RESTful API（/api/v1，35 个领域路由）                      │
│  · Web 运营控制台与静态资源托管                               │
│  · 登录鉴权 · 钉钉 Stream 双向机器人 · 模型网关代理            │
└───────────────▲──────────────────────────────────────────────┘
                │ (HTTP API, API-only 无直连 DB)
┌───────────────┴──────────────────────────────────────────────┐
│  axiom runner  (调度器 + 心跳)                                │
│  · Worker 领取 Lease、阶段执行与超时管控                      │
│  · 子进程 Pi Agent 启动与会话跟踪                             │
│  · Browser Broker 原生浏览器沙箱管理                          │
└──────────────────────────────────────────────────────────────┘
```

- **`axiom serve`**：控制面 + 数据面。业务事务落库、会话鉴权、前端交互、模型请求代理（Worker 的 LLM 请求经 serve 反向代理至上游 provider，`api_key` 只存 serve 侧）；
- **`axiom runner`**：执行面。不连数据库，仅通过 `/api/v1/dispatch/*` 领取执行租约并驱动各阶段智能体；
- **PostgreSQL**：任务、图、事实、意图、漏洞、审计、Token 用量等全部状态的唯一事实源（41 张必需表；运行时硬性要求 PostgreSQL，SQLite 仅作为历史数据一次性导入源保留）。

## 2. 五阶段流水线

任务生命周期遵循确定性状态流转，每个阶段由独立 Worker 承载：

```text
[ 新建任务 (Task) ]
        │
        ▼
 1. Bootstrap（初始测绘）
    ├── 收集静态/动态 JS Chunk、SPA 路由与 Source Map
    ├── 识别 API 前缀域，展开完整 API 能力树
    └── 标定初始鉴权矩阵与基线对象 ID
        │
        ▼
 2. Reason（增量推理规划）
    ├── 维护动态 Todo 树与 Intent 派发
    ├── 单账号「自建自测闭环」，严禁偷懒式 Blocked
    └── 关联新 Fact 事实，评估阶段 Goal 达成状态
        │
        ▼
 3. Explore（动态实操与验证）
    ├── 调度 Browser Broker 原生浏览器沙箱深层交互
    ├── 自动提取 Cookie 与 JWT 凭据（browser_get_credentials）
    └── 输出结构化证据、复现步骤与待验证漏洞报告
        │
        ▼
 4. Plan（增量漏洞规划）
    └── 基于已确认事实规划下一轮攻击面与验证路径
        │
        ▼
 5. Review（独立交叉复现审计）
    ├── 对上报漏洞做独立端到端复现
    └── 剔除误报，给出真实裁决（已验证 / 忽略 / 重复）与状态理由
```

**漏洞判定原则：危害驱动（Impact-Driven）。** 没有真实危害证据的空壳发现会被 Review 阶段直接剔除。

## 3. Web 运营控制台

服务自带单页控制台：`index.html` 为 SPA 壳（Alpine.js），按 hash 路由动态挂载模板片段。样式三层叠加——`apple.css` 基础设计 tokens（亮/暗双主题，`.dark` 显式切换）+ `custom.css` 的 `sto-*` 组件扩展层 + Tailwind 工具类。共 **12 个侧栏页面**：

| 页面 | 能力 |
|---|---|
| 仪表盘 | 全局统计、任务分布、事件流、Token 消耗面板 |
| 任务管理 | 任务列表（卡片自带生命体征点与事实/意图统计）、任务详情、诊断折叠区、执行配置、操作区 |
| 漏洞管理 | 严重度色彩体系（critical/high/medium/low）、状态流转、漏洞包导出 |
| 项目管理 | 项目分组与任务归属 |
| 守门审计 | Worker 命令审计流、按裁决着色、暗色命令块 |
| Worker 管理 | 主从布局：Worker 阵容 / 模型后端；并发槽位、思考强度、模型选择与上下文窗口 |
| 工具管理 | 分段控制器 + 阶段能力树 |
| Skill 管理 | 三栏布局：技能列表 / 文件树 / 编辑器 |
| 产物中心 | 任务侧栏 + 文件夹/产物卡 + 预览抽屉（Markdown 渲染 / 代码高亮） |
| MCP 管理 | Server 导入、工具发现与浏览、启用开关 |
| 提示词 | 各阶段 Prompt 查看与管理 |
| 设置 | 常规 / 派发 / 存储治理 / 集成 / 裁判管理（守门策略） 五分区 |

任务详情页提供三种视图：**人工视图**（实时对话与插话）、**图谱视图**（Fact/Intent 图谱可视化、视口裁剪、阶段着色）、**归档回看**（只读）。

登录鉴权：首次启动自动创建管理员（用户名取 `AXIOM_ADMIN_USER`，默认 `axiom`；密码取 `AXIOM_ADMIN_PASSWORD`，未设置则随机生成 16 位强密码并写入数据目录）。

## 4. 核心子系统

### 4.1 Worker 池与模型后端
- Worker 按阶段（bootstrap/reason/explore/plan/review）分配能力，并发槽位为全局池，页面上分阶段数字仅作占用展示；
- 每个 Worker 绑定独立模型后端（Pi 兼容网关：`base_url` + `api_key`），支持模型发现、上下文窗口标注、思考强度调节；
- Worker 的 LLM 请求统一经 serve 的模型网关反代至上游 provider，避免 Worker 直连外网、`api_key` 不落执行面；
- Provider 错误分类与 Worker 冷却机制，避免故障扩散。

### 4.2 Browser Broker（原生浏览器沙箱）
- 独立调度受控 Chrome 实例（数据目录 `~/Library/Application Support/Chrome-SecTest`），与日常浏览器物理隔离；
- AF_UNIX socket 服务 + Playwright 会话桥：页面快照解析、DOM 交互、HTTP/WebSocket 网络抓包监控，共 20 个 `browser_*` 工具；
- `browser_get_credentials` 按目标域名自动提取 Cookie（含 HttpOnly）与 LocalStorage/SessionStorage Token，格式化为标准 HTTP 请求头。

### 4.3 任务级执行沙箱
- 每个任务恰好绑定一个执行沙箱（TaskSandboxManager），实现任务间的工作目录与执行环境隔离；
- 双后端可插拔：`local`（本机受控目录）与 `cube`（E2B 兼容 API 的云沙箱；策略要求强制沙箱而后端不可用时 fail-closed）；
- 执行装配时注入任务级 Prompt 上下文（任务配置、凭据指引等），五个阶段提示词均支持。

### 4.4 内置技能库（16 项）
`dispatcher/skills/` 内置：`nmap`、`subfinder`、`httpx`、`katana`、`ffuf`、`nuclei`、`sqlmap`、`whatisvul`、`vuln-report`、`vuln-review`、`xss-verify`、`business-logic-testing`、`browser-control`、`credential-refresh`、`miniapp`、`agent-mail`。技能以文件树形式在控制台可查看与编辑。

### 4.5 守门审计（Guard）
- 对 Worker 发出的敏感命令做策略裁决（放行 / 拦截 / 确认）：Pi 侧 `axiom-guard` 扩展在每次 tool_call 前置请求 serve 裁决，BLOCK 即拦截；
- 默认策略内置（白名单 / 硬规则 / 裁判提示词），在控制台设置页「裁判管理」分区配置；
- 审计事件全量落库，控制台按裁决着色展示、可展开原始命令。

### 4.6 漏洞管理与独立评审
- 漏洞状态机：上报 → 评审 → 定论，Reviewer 独立端到端复现；
- 严重度体系与漏洞包一键导出（Markdown 报告 + 证据附件）；
- 评审理由（`status_reason`）随钉钉卡片推送。

### 4.7 MCP 管理
- MCP Server 发现与导入，工具列表浏览与启用控制，Schema 顺序归一化。

### 4.8 存储治理
- 三档清理中心：T1 一键清理（流水日志等安全类别）/ T2 预览确认 / T3 锁定展示（不可清理）；
- 堆叠比例概览、日志轮转配置。

### 4.9 Token 用量
- 按 Intent / 模型 / 阶段统计 prompt 与 completion 用量、缓存命中率；
- 未缓存输入 Top 排行，定位烧钱热点；支持从 Pi Session JSONL 离线回补（`axiom token-usage-backfill`）。

### 4.10 钉钉机器人
- **两阶段精准推送**：漏洞初报（复现 Curl + 受影响资产）→ 评审定论（最终状态 + 真实理由）；
- **群内双向指令**（Stream 长连接）：`/help`、`/tasks`、`/status <任务ID>`、`/pause`、`/resume`、`/vulns <任务ID>`、`/vuln <漏洞ID>`。

### 4.11 人工干预与可观测
- Explore 实时对话与人工插话（interventions）；
- 执行动作回放、活动追踪、诊断中心（Worker 成功率 / 失败超时排行）；
- 运维脚本：PostgreSQL 备份 / 快照比对 / 恢复演练 / 观察者（`ops/`）。

## 5. 快速部署

### 5.1 环境要求
- macOS（Apple Silicon 推荐）或 Linux
- Python 3.12+（推荐 `uv` 包管理器）
- Docker（PostgreSQL 18.4，镜像按 digest 固定）

### 5.2 启动

```bash
# 1. 启动 PostgreSQL（含自动建库与最小权限角色初始化）
docker compose -f compose.yaml -f compose.host.yaml up -d postgres

# 2. 同步依赖
uv sync --frozen

# 3. 执行数据库迁移
.venv/bin/axiom db upgrade

# 4. 启动 API + Web 控制台
.venv/bin/axiom serve --host 127.0.0.1 --port 8899 --data-dir ~/.local/share/axiom-v3 &

# 5. 启动调度 Runner
.venv/bin/axiom runner --api-url http://127.0.0.1:8899 &
```

访问 `http://127.0.0.1:8899` 进入控制台。

### 5.3 常驻守护
生产环境建议以 `launchd`（macOS）/ `systemd`（Linux）托管两个进程；`ops/postgres/` 内置备份与观察者的 LaunchAgent 安装脚本。

### 5.4 一行命令 Docker 部署（含 Windows）

三件套（PostgreSQL + API/控制台 + 调度 Runner，镜像内含 pi Worker 运行时与 Chromium）：

```bash
docker compose -f compose.deploy.yaml up -d --build
```

- 国内网络构建可传镜像源（可选）：

```bash
docker compose -f compose.deploy.yaml build \
  --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  --build-arg UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
  --build-arg PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
docker compose -f compose.deploy.yaml up -d
```

- 首次启动自动完成：数据库迁移（migrator 角色）→ 管理员引导（`AXIOM_ADMIN_USER`/`AXIOM_ADMIN_PASSWORD`，未传密码则随机生成并写入数据卷 `credentials.txt`）→ service_token 生成（Runner 与 Worker 网关模式自动读取）。
- Worker（pi 运行时 + Chromium）在 runner 容器内原生运行，浏览器为 headless（`CHROME_HEADLESS=1`、容器内自动 `--no-sandbox`）。
- 控制台宿主机端口默认 `18888`（容器内 8888，可用 `AXIOM_PORT` 覆盖），部署后访问 `http://127.0.0.1:18888`，在系统设置里配置 LLM Provider 即可开工。

## 6. 一键升级

```bash
./scripts/upgrade.sh              # 本地升级
./scripts/upgrade.sh --force      # 强制暂存本地修改并升级
```

安全机制：数据库连通性检查 → 拉取最新代码 → `uv sync` → `axiom db upgrade`（失败自动回滚）→ 平滑重启守护进程 → 健康检查确认。

## 7. 常用 CLI

```bash
axiom serve                        # 启动 API + 控制台（默认 127.0.0.1:8899）
axiom runner --api-url http://127.0.0.1:8899   # 启动调度 Runner
axiom db upgrade                   # PostgreSQL Alembic 迁移
axiom db migrate-sqlite --sqlite-path <path>   # 历史 SQLite 库一次性导入 PostgreSQL
axiom maintenance status|set       # 查看/设置 drain/frozen 维护态
axiom skill path <name>            # 打印技能目录
axiom skill doctor <name>          # 技能排障
axiom credential|reason|plan|explore|review    # Worker 租约侧命令组
axiom token-usage-backfill         # 从 Pi Session JSONL 回补 Token 用量
axiom dingtalk test                # 钉钉连通性测试
```

## 8. 仓库结构

```text
axiom/
├── compose.yaml / compose.host.yaml / compose.test.yaml   # PostgreSQL 容器编排
├── compose.deploy.yaml / Dockerfile                       # 三件套一键部署（含 pi 运行时 + Chromium）
├── pyproject.toml / uv.lock                               # 依赖（FastAPI/SQLAlchemy/Playwright/MCP 等）
├── alembic.ini / migrations/                              # PostgreSQL 迁移链（pg0001→…）+ 遗留 SQLite 链参照
├── scripts/upgrade.sh                                     # 一键升级
├── ops/postgres/                                          # 备份、快照、恢复演练、观察者
├── docs/                                                  # 按日期归档的设计与变更文档
├── src/axiom/
│   ├── api/                    # 路由汇聚（35 个领域路由）
│   ├── app.py                  # FastAPI 应用与静态资源托管
│   ├── composition.py          # DI 容器：装配全部领域 Service
│   ├── cli.py / cli_runner.py / worker_cli.py / skill_cli.py
│   ├── dispatcher/             # 调度引擎：runner、五阶段执行器、browser_broker、
│   │                           #   prompts/（阶段提示词）、skills/（内置技能库）、extensions/（Pi 扩展）
│   ├── modules/                # 领域模块：projects/tasks/goals/reason/plan/graph/todos、
│   │                           #   workers/dispatch/execution/phase_capabilities/diagnostics、
│   │                           #   vulnerabilities/vulnerability_reviews/guard、
│   │                           #   skills/mcp/axiom_tools/knowledge/context/credentials、
│   │                           #   sandboxes/artifacts/archives/exports/storage_admin/token_usage、
│   │                           #   interventions/dashboard/settings/auth/audit/integrations、
│   │                           #   llm_gateway（模型网关反代）
│   ├── shared/                 # 共享底座（database、storage、dingtalk、ids）
│   ├── migration/              # SQLite → PostgreSQL 一次性导入器
│   └── static/                 # Web 控制台（index.html SPA + js/ + templates/ + apple.css/custom.css）
└── tests/                      # 763 个测试用例
```

## 9. 测试

```bash
.venv/bin/python3 -m pytest -q        # 763 个用例（单元 + API + 前端读模型 + 前端资产一致性）
```

覆盖：调度器与租约、五阶段生命周期、浏览器 Broker、任务沙箱、守门策略、漏洞评审状态机、存储治理、Token 解析、钉钉指令、认证会话、前端资产版本一致性等。

## 10. 安全与合规声明

AXIOM 仅用于**获得明确书面授权**的安全评估场景。使用者须遵守当地法律法规，对目标系统具备合法测试授权；因滥用本工具造成的任何后果由使用者自行承担。
