# AXIOM V2 — 自动化渗透测试与智能安全调度系统

AXIOM V2 是面向企业级安全测试与漏洞挖掘的多智能体协同渗透系统。基于 **PostgreSQL 原生架构**、**自动化状态机调度**、**动态浏览器沙箱 (Browser Broker)** 与 **多通道告警集成** 构建，支持自动化资产测绘、API 逆向分析、漏洞验证闭环与报告生成。

---

## 🚀 核心架构与特性

- **多智能体协作流水线**：
  - `Bootstrap`：入口探查、动态 chunk 收集、API 前缀域抽取、业务能力树还原。
  - `Reason`：增量推理规划、Todo 树维护、单向自测与攻击面拓展、状态机评估。
  - `Explore`：动静结合漏洞验证、真实浏览器交互抓包与凭证提取、高并发探测。
  - `Review`：多维度交叉独立复现、防误报裁决与审计定级。
- **Browser Broker 原生浏览器沙箱**：
  - 独立运行 `Google Chrome for Testing`，与宿主日常 Chrome 物理级隔离。
  - 支持 CDP 代理、页面快照、DOM 交互、网络抓包与自动凭证提取（`browser_get_credentials`）。
- **钉钉机器人深度集成 (DingTalk Webhook & Stream)**：
  - **双向指令交互**：群内发送 `/help`、`/tasks`、`/status`、`/pause`、`/resume`（默认不限时运行）、`/vulns` 等指令实时查询与控制。
  - **漏洞精准推送闭环**：探针上报时推送**发现初报卡片**；评审裁决后推送**定论与裁决理由卡片**。
- **一键平滑热升级**：自带 `scripts/upgrade.sh`，支持依赖自动同步、Schema 幂等迁移与超时自动回滚。

---

## ⚡ 快速开始

### 1. 运行环境要求
- **操作系统**：macOS (推荐 Apple Silicon ARM64) 或 Linux
- **Python**：Python 3.11+（建议使用 `uv` 包管理器）
- **数据库**：PostgreSQL 16+（支持 Docker 容器或本地实例，默认端口 `55432`）

### 2. 本地一键安装与启动

```bash
# 1. 克隆代码仓库
git clone https://github.com/notstarr/AXIOM.git
cd AXIOM

# 2. 启动 PostgreSQL (Docker Compose)
docker compose -f compose.yaml -f compose.host.yaml up -d postgres

# 3. 初始化 Python 虚拟环境与依赖
uv sync --frozen
# 或使用 pip:
# python3 -m venv .venv && source .venv/bin/activate && pip install -e .

# 4. 执行数据库 Schema 初始化
.venv/bin/axiom db upgrade

# 5. 启动服务
# 启动 Web 服务 (端口 8888)
.venv/bin/axiom serve --host 127.0.0.1 --port 8888 &
# 启动后台任务调度 Runner
.venv/bin/axiom runner --api-url http://127.0.0.1:8888 &
```

---

## 🔄 系统升级 (一键升级脚本)

远端部署或日常升级可直接运行自带的平滑升级脚本：

```bash
# 方式 A：本地项目内直接执行
./scripts/upgrade.sh

# 方式 B：强制暂存本地修改并升级
./scripts/upgrade.sh --force

# 方式 C：远程一键管道升级
curl -fsSL https://raw.githubusercontent.com/notstarr/AXIOM/main/scripts/upgrade.sh | bash
```

**升级脚本自动执行以下 6 步保障流程：**
1. 检查 Docker / PostgreSQL 连接与工作区安全；
2. 拉取远端最新 `main` 分支代码；
3. 同步虚拟环境 Python 依赖 (`uv sync`)；
4. 执行 Alembic 数据库 Schema 幂等迁移；
5. 平滑重启后台守护进程（macOS `launchd` / Linux `systemd`）；
6. 自动执行 API 连通性健康检查（超时自动代码回滚）。

---

## 🤖 钉钉机器人与告警配置

### 1. Web 控制台配置
访问 `http://127.0.0.1:8888`，进入 **「设置」➔「钉钉告警」**：
- 开启 **「钉钉通知与指令交互」** 卡片开关；
- 填入钉钉群机器人的 **Webhook URL** 与可选的 **加签 Secret**；
- 点击 **「发送测试通知」** 验证连通性。

*(也可以直接配置环境变量 `AXIOM_DINGTALK_WEBHOOK_URL` 与 `AXIOM_DINGTALK_SECRET`)*

### 2. 钉钉群指令大全

在钉钉群中 `@机器人` 发送指令即可：

| 指令 | 说明 | 示例 |
| :--- | :--- | :--- |
| `/help` | 查看完整的分类指令指南与帮助 | `/help` |
| `/tasks` | 列出当前所有正在运行的任务及进度 | `/tasks` |
| `/status <任务ID>` | 查看指定任务的当前阶段、已跑时长与目标 | `/status 172` 或 `/status t_172` |
| `/pause <任务ID>` | 暂停指定任务 | `/pause 172` |
| `/resume <任务ID>` | 恢复/继续执行任务（**默认不限时持续运行**） | `/resume 172` |
| `/vulns <任务ID>` | 查看该任务挖掘出的所有漏洞清单与评审状态 | `/vulns 172` |
| `/vuln <漏洞ID>` | 查看单个漏洞的详细复现 Curl、描述与评审意见 | `/vuln v008` |

---

## 🛠️ 常用 CLI 命令

```bash
# 查看系统状态
.venv/bin/axiom --help

# 数据库迁移升级
.venv/bin/axiom db upgrade

# 钉钉连通性测试
.venv/bin/axiom dingtalk test

# 查看 Token 使用量统计
.venv/bin/axiom token-usage
```

---

## 📂 核心目录说明

- `src/axiom/dispatcher/`：任务调度引擎、提示词工程与多智能体执行器。
- `src/axiom/modules/`：各领域业务模型（tasks, todos, vulnerabilities, settings 等）。
- `src/axiom/shared/`：公共底座（database, dingtalk, storage, ids 等）。
- `scripts/`：运维工具与升级脚本 (`upgrade.sh`)。
- `~/.local/share/axiom-v3/`：运行时产物、日志 (`logs/api.log`)、上下文与凭证存储。
