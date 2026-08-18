# AXIOM V2 — 全自动智能安全渗透与漏洞挖掘系统

AXIOM V2 是面向企业级安全测试的多智能体协同自动化渗透平台。系统基于 **PostgreSQL 原生架构**、**确定性状态机调度**、**动态浏览器沙箱 (Browser Broker)** 与 **多通道实时告警** 构建，具备完整的全生命周期渗透测试能力：从目标资产测绘、API 逆向与能力树抽取，到增量推理规划、动态交互验证与多维度交叉复现审计。

---

## 🌟 核心架构与工作流

AXIOM 采用严密的多阶段智能体协作流水线，阶段间通过结构化 Fact、Todo 与 Intent 流转：

```text
[ 目标输入 ] ➔ 1. Bootstrap ──(资产能力树/线索)──> 2. Reason
                                                     │   ▲ (新事实/反馈)
                                 (规划 Intent 派发)   ▼   │
                                                3. Explore
                                                     │
                                 (上报待验证漏洞)     ▼
                                                4. Review ──(验证/忽略/重复)──> 5. 最终漏洞成果
```

### 1. 阶段职责
- **`Bootstrap`（引导测绘）**：
  - SPA/Web、微信小程序及移动端资产自动化逆向；
  - 深度解析 JS Bundle，提取 API 前缀域、底座拼装函数并展开**完整 API 能力树**；
  - 标定鉴权矩阵与基线对象 ID，禁止盲目猜测。
- **`Reason`（增量推理与规划）**：
  - 维护动态 Todo 树与 Intent 调度；
  - 强制落实**自建自测闭环（Self-Creation Loop）**与单向攻击面，严禁偷懒式 Blocked；
  - 动态评估 Goal 达成条件与收口状态。
- **`Explore`（动态探索与实操）**：
  - 结合真实浏览器沙箱与自动化网络探测，执行深度漏洞挖掘；
  - 自动提取目标会话凭据（Cookie / LocalStorage / Bearer Token）；
  - 输出可复现的 Proof-of-Concept 及结构化证据链。
- **`Review`（独立交叉复现与审计）**：
  - 对探针上报的漏洞进行独立端到端复现；
  - 严格剔除虚假回显、框架报错等误报，裁决漏洞最终状态（`已验证` / `忽略` / `重复`）。

---

## 🛡️ 核心子系统

### 1. Browser Broker 原生浏览器沙箱
- **物理级隔离**：独立调度 `Google Chrome for Testing`（或专属 `Chrome-SecTest` 数据目录），彻底隔绝日常宿主 Chrome 进程；
- **全能力支持**：支持页面打开、DOM 快照分析、按键点击、网络抓包监控、控制台监听；
- **自动凭证提取 (`browser_get_credentials`)**：自动按目标域名/页面提取 Cookie、LocalStorage 中的 JWT，并拼装为标准 HTTP 请求头。

### 2. Guard 实时安全守门引擎
- 双层防护机制：前置正则表达式硬规则（秒级阻断危险破坏性命令） + 后置 LLM 语义实时审计；
- 全局监控探针的外部命令执行，防止越界或破坏性操作。

### 3. 钉钉告警与群交互机器人
- **两阶段精准推送闭环**：
  - *【阶段 1 · 发现初报】*：探针上报待验证漏洞时，立即推送包含复现 Curl 与受影响目标的告警卡片；
  - *【阶段 2 · 评审定论】*：Reviewer 裁决完毕后，立即推送包含最终结论与**评审裁决理由**的卡片。
- **群聊交互控制**：在钉钉群发送 `/help`、`/tasks`、`/status`、`/pause`、`/resume` 等指令直接管理系统。

---

## ⚡ 快速部署与启动

### 环境要求
- **系统**：macOS (推荐 Apple Silicon ARM64) 或 Linux
- **Python**：Python 3.11+（推荐使用 `uv`）
- **数据库**：PostgreSQL 16+（默认宿主端口 `55432`）

### 启动步骤

```bash
# 1. 克隆代码仓库
git clone https://github.com/notstarr/AXIOM.git
cd AXIOM

# 2. 启动 PostgreSQL 数据库容器
docker compose -f compose.yaml -f compose.host.yaml up -d postgres

# 3. 安装 Python 依赖
uv sync --frozen

# 4. 执行数据库 Schema 迁移
.venv/bin/axiom db upgrade

# 5. 启动后台守护进程
# 启动 Web 控制台与 API（默认 127.0.0.1:8888）
.venv/bin/axiom serve --host 127.0.0.1 --port 8888 &

# 启动任务调度执行器 Runner
.venv/bin/axiom runner --api-url http://127.0.0.1:8888 &
```

访问 `http://127.0.0.1:8888` 即可进入控制台。

---

## 🔄 一键平滑升级

AXIOM 内置自动化升级管理脚本，支持依赖同步、Schema 幂等升级、服务平滑重启与健康检查回滚：

```bash
# 本地项目一键升级
./scripts/upgrade.sh

# 强制暂存本地修改升级
./scripts/upgrade.sh --force

# 远程管道一键升级
curl -fsSL https://raw.githubusercontent.com/notstarr/AXIOM/main/scripts/upgrade.sh | bash
```

**升级安全管道：**
1. 校验 Docker 与 PostgreSQL 连接；
2. 拉取远端最新代码 (`git pull --ff-only`)；
3. 同步 Python 依赖 (`uv sync`)；
4. 运行 `axiom db upgrade` 执行数据库迁移（失败自动 `git reset` 回滚）；
5. 平滑重启后台 `serve` 与 `runner` 服务（macOS `launchd` / Linux `systemd`）；
6. 轮询 API 进行健康检查确认升级成功。

---

## 🤖 钉钉机器人集成指南

### 1. 配置 Webhook
进入 Web 控制台 **「设置」➔「钉钉告警」**：
1. 打开 **「已开启钉钉通知与群指令」** 卡片开关；
2. 填入钉钉自定义机器人的 **Webhook URL** 和 **加签 Secret**；
3. 点击 **「保存钉钉配置」**；
4. 点击 **「发送测试通知」** 验证群内卡片联通性。

### 2. 钉钉群指令列表

群内 `@机器人` 或私聊发送以下指令（任务 ID 支持纯数字简写）：

| 分类 | 指令 | 功能说明 | 示例 |
| :--- | :--- | :--- | :--- |
| **帮助** | `/help` | 查看完整的分类指令指南 | `/help` |
| **监控** | `/tasks` | 列出当前所有正在运行的任务及简报 | `/tasks` |
| **监控** | `/status <ID>` | 查询指定任务阶段、已跑时长与目标 | `/status 172` 或 `/status t_172` |
| **控制** | `/pause <ID>` | 暂停指定任务 | `/pause 172` |
| **控制** | `/resume <ID>` | 继续/恢复任务（**默认不限时持续运行**） | `/resume 172` |
| **漏洞** | `/vulns <ID>` | 查看指定任务挖掘出的所有漏洞成果清单 | `/vulns 172` |
| **漏洞** | `/vuln <ID>` | 查看单个漏洞详情、复现 Curl 与评审意见 | `/vuln v008` |

---

## 🛠️ CLI 命令行速查

```bash
# 查看系统指令帮助
axiom --help

# 数据库 Schema 升级
axiom db upgrade

# 钉钉通知连通性测试
axiom dingtalk test

# 查看模型 Token 消耗与统计
axiom token-usage
```

---

## 📁 仓库结构

```text
axiom/
├── compose.yaml                # PostgreSQL 服务容器配置
├── scripts/
│   └── upgrade.sh              # 一键自动化升级与自愈脚本
├── src/axiom/
│   ├── api/                    # RESTful API 路由分发
│   ├── dispatcher/             # 调度引擎、状态机、多智能体执行器与提示词
│   │   ├── browser_broker.py   # Browser Broker 原生浏览器沙箱
│   │   ├── prompts/            # Bootstrap, Reason, Explore, Review 核心提示词
│   │   └── skills/             # 内置安全技能库 (whatisvul, ffuf, xss-verify 等)
│   ├── modules/                # 业务领域模块 (tasks, todos, vulnerabilities 等)
│   │   └── integrations/       # 钉钉 Webhook 与交互指令 API
│   ├── shared/                 # 底座共享库 (database, dingtalk, storage 等)
│   └── static/                 # 前端 Web 控制台与适配层 (v2-adapter.js)
└── tests/                      # 单元与端到端集成测试集
```
