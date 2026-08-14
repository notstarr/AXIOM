# AXIOM Linux 一键部署

AXIOM 的正式 Linux 发行版是一个 **Docker Compose 项目**，不是 SQLite 数据目录的打包副本。它在一台干净的 Linux x86_64 主机上初始化全新的 PostgreSQL，并启动：

```text
postgres  →  migrator  →  api  →  runner
```

API 同时托管前端，因此目标机器不需要单独安装 Node、Nginx 或前端开发依赖。Runner 镜像内置 Pi、Node、Chromium、浏览器/Guard/MCP/Tool Extension、仓库 Skill 和已验证的安全测试 CLI。

> 当前版本只支持 Linux x86_64，目标系统为 Ubuntu 22.04/24.04 + Docker Engine + Docker Compose Plugin。当前开发机是 macOS arm64，不能替代 Linux amd64 实机验收；真实 Linux Pi runtime 也必须由发布制品注入，不能拿 macOS runtime 顶替。

## 一行安装

发布后：

```bash
curl -fsSL https://raw.githubusercontent.com/notstarr/AXIOM/main/deploy/install.sh | bash
```

安装脚本会：

1. 检查 Linux、x86_64、Docker、Compose、Docker daemon、端口和磁盘空间；
2. 创建 `/opt/axiom`（可用 `AXIOM_INSTALL_DIR` 覆盖）；
3. 下载固定 release 的 Compose、环境模板、控制脚本和 PostgreSQL 初始化脚本；
4. 生成 PostgreSQL 角色密码和 Runner 内部服务 Token，并将 `.env` 设为 `0600`；
5. 拉取固定版本镜像，并将应用镜像解析为 Registry digest；
6. 启动 PostgreSQL，等待健康检查；
7. 执行当前 PostgreSQL Alembic 分支 `postgresql@head`；
8. 启动 API 和 Runner，等待 `/health` 和 Runner 健康检查；
9. 输出访问地址、安装目录和管理命令。

默认地址：

```text
http://127.0.0.1:8888
```

安装过程不执行 SQLite 导入，不恢复旧任务，不复制旧 Artifact，也不读取旧机器的数据卷。旧数据若需要迁移，继续使用 AXIOM 的独立 `axiom db migrate-sqlite` 工具，不会被一键部署隐式调用。

### 审查后安装

不想直接执行管道命令时：

```bash
curl -fsSLO https://raw.githubusercontent.com/notstarr/AXIOM/main/deploy/install.sh
less install.sh
bash install.sh
```

可指定安装目录、端口或发布引用：

```bash
AXIOM_INSTALL_DIR=/srv/axiom AXIOM_PORT=8988 \\
  AXIOM_RELEASE_REF=v0.1.0 bash install.sh
```

## 安装目录和数据卷

```text
/opt/axiom/
├── compose.yaml
├── .env                 # 0600，包含明文配置和密码
├── axiomctl
├── VERSION
├── backups/
└── logs/
```

用户数据不放在安装目录，使用独立 Docker Volume：

```text
axiom-postgres
└── PostgreSQL 18 数据库

axiom-data
├── runs/
├── artifacts/
├── workspace/
├── skills/
├── axiom-tools/
├── maintenance.json
└── 其他运行时数据

axiom-browser-profile
└── Chromium headless Profile 和缓存
```

普通 `uninstall` 只删除容器和镜像，保留上述 Volume。只有明确执行以下命令才会删除任务、Artifact、Skill、Workspace、浏览器 Profile 和数据库：

```bash
axiomctl uninstall --purge
```

`--purge` 要求两次确认：输入 `DELETE`，再输入完整安装目录。

## 服务和网络

### PostgreSQL

- 使用固定 digest 的官方 PostgreSQL 18.4 镜像；
- 默认不映射宿主机数据库端口；
- 使用 `axiom-postgres` Volume；
- 首次初始化 `axiom_owner`、`axiom_migrator`、`axiom_app` 和 `axiom` Schema；
- `axiom_owner` 不登录，迁移使用 `axiom_migrator`，API/Runner 使用 `axiom_app`；
- 公共 `public` Schema 不允许普通公共 CREATE；
- 数据库连接和时间默认使用 UTC。

### Migrator

`migrator` 使用与 API 相同的 AXIOM 镜像执行：

```bash
axiom db upgrade
```

只有迁移容器成功退出后 API 才会启动。空数据库默认安装到当前 PostgreSQL Head；不执行 SQLite 历史迁移。

### API

容器内部监听：

```text
0.0.0.0:8899
```

默认宿主机映射：

```text
127.0.0.1:8888 → api:8899
```

浏览器访问宿主机端口，Runner、Pi Extension 和内部工具访问：

```text
http://api:8899
```

### Runner

Runner 是独立容器，通过 Compose 网络调用 API，不直接连接 PostgreSQL：

```bash
axiom runner --api-url http://api:8899 --service-token "$AXIOM_SERVICE_TOKEN"
```

API、Runner 和工作区共享 `axiom-data`；Runner 额外挂载 `axiom-browser-profile`。容器整体以非 root 用户运行。

## Runtime、浏览器和工具链

镜像固定包含：

```text
Pi                         0.82.1
Node                       v22.22.2
Chromium                   容器内 headless Chromium
ffuf                       v2.1.0
katana                     v1.1.3
httpx                      v1.6.10
subfinder                  v2.6.7
nuclei                     v3.3.7
curl / jq / python / nmap / sqlmap
```

Pi runtime 由 `runtime/manifest.json` 描述，并在 CI 和最终镜像中强制验证：Linux、x86_64、Pi/Node 版本、package name、`bin.pi` 入口、Pi/Node SHA-256 和可执行权限都不通过时构建直接失败。运行时根目录由 `AXIOM_RUNTIME_ROOT` 指定，生产镜像为：

```text
/opt/axiom/runtime
```

不能将开发机的 `/Users/...`、Homebrew Node 或 macOS Pi runtime 写入 Worker 配置。旧 Worker 中的历史绝对路径由 canonicalize 逻辑转换到镜像内 runtime 路径。

Browser Broker 默认使用容器内 Chromium：

```text
CHROME_HEADLESS=1
CHROME_BIN=/usr/bin/chromium
CHROME_NO_SANDBOX=1
AXIOM_CHROME_PROFILE_DIR=/var/lib/axiom-browser-profile
```

浏览器只连接容器内自身 CDP，不开放任意远程浏览器连接。默认容器启用
`CHROME_NO_SANDBOX=1` 以兼容 Docker 的用户命名空间限制；AXIOM 镜像本身仍以
非 root 用户运行。若宿主机已经允许 Chromium sandbox，可将该值改为 `0`。

## MCP 和 Reqable

Reqable **不是部署前置依赖，也不会自动安装**。没有配置 MCP 时 Runner 仍应正常启动；已配置的 MCP 由现有 AXIOM 管理页面和数据库配置控制，部署没有改动 MCP/Skill/Worker 协议。

外部 MCP 如果运行在宿主机上，容器内不能用宿主机的 `127.0.0.1`。Compose 已为 API/Runner 提供：

```text
host.docker.internal → host-gateway
```

因此 MCP 地址通常应填写宿主机可达地址，Linux Docker 环境可优先尝试 `host.docker.internal:<port>`。这只是网络可达性处理，仍需在 AXIOM 页面显式配置 MCP Server 和要开放的具体 Tool；Reqable 是否可用取决于外部服务的监听地址、证书和防火墙。

## 管理命令

在安装目录执行，或安装成功后直接执行 `axiomctl`：

```bash
axiomctl install                 # 幂等初始化并启动
axiomctl start                   # 启动 PostgreSQL、迁移、API、Runner
axiomctl stop                    # 停止服务，不删除数据
axiomctl restart                 # 迁移后重建 API/Runner
axiomctl status                  # 服务、健康、版本、Volume、迁移状态
axiomctl logs                    # 跟踪全部服务日志
axiomctl logs api                # 跟踪单个服务日志
axiomctl update                  # 按当前 .env 的固定镜像更新
axiomctl update 0.2.0            # 更新到指定 release version
axiomctl update 0.2.0 IMAGE      # 使用指定镜像更新
axiomctl pin-image               # 将已拉取的 Registry tag 固化为 digest
axiomctl rollback                # 恢复上一次 .env/镜像配置
axiomctl backup                  # 生成 PostgreSQL custom/global dump
axiomctl doctor                  # Docker、端口、DB、迁移、Runtime、工具诊断
axiomctl uninstall               # 删除容器和镜像，保留数据卷
axiomctl uninstall --purge       # 二次确认后删除安装和全部数据
```

`start`、`restart`、`update` 都会先确保 PostgreSQL 健康，再执行迁移；API 依赖迁移器成功完成。`update` 会先备份当前 `.env`，拉取新镜像、固化 digest、执行迁移，然后重建 API/Runner。迁移失败或健康检查失败时会恢复旧镜像配置，但**不会自动降级数据库**。

`rollback` 只恢复应用镜像/配置，不自动降级 Alembic。只有发布清单确认旧应用兼容当前数据库 Schema 时才可使用；已经执行不可逆迁移的版本禁止盲目回滚。

## 配置

主要变量：

```text
AXIOM_VERSION
AXIOM_IMAGE
AXIOM_POSTGRES_IMAGE
AXIOM_PUBLISH_HOST
AXIOM_PORT
AXIOM_DATA_DIR
AXIOM_DB_HOST / AXIOM_DB_PORT / AXIOM_DB_NAME
AXIOM_DB_MIGRATOR_PASSWORD
AXIOM_DB_APP_PASSWORD
AXIOM_POSTGRES_BOOTSTRAP_PASSWORD
AXIOM_SERVICE_TOKEN
AXIOM_INTERNAL_SERVER_URL
CHROME_HEADLESS / CHROME_BIN / CHROME_NO_SANDBOX / AXIOM_CHROME_PROFILE_DIR
```

密码、API Key、MCP 配置和 Worker 配置按当前行为明文保存，不增加应用层加密、脱敏、外部 Secret 或主密钥体系。`.env`、备份目录和日志目录应限制为部署管理员可读，安装日志不会主动打印这些值。

默认仅监听 `127.0.0.1:8888`。如果确实需要局域网访问：

```bash
sed -i 's/^AXIOM_PUBLISH_HOST=.*/AXIOM_PUBLISH_HOST=0.0.0.0/' /opt/axiom/.env
axiomctl restart
```

不要直接暴露公网；没有完整用户认证时，应使用防火墙、反向代理或 SSH 隧道。

## 镜像构建和发布

正式安装使用已发布的预构建镜像，目标机器不需要 Node、Go、Python、Pi 或 Chromium 构建环境。

本地构建必须先准备真实 **Linux x86_64** runtime，详见 `runtime/README.md`。Dockerfile 要求所有基础镜像通过 build arg 传入不可变 digest；示例：

```bash
docker buildx build \\
  --build-arg AXIOM_NODE_BASE_IMAGE='node:22.22.2-bookworm-slim@sha256:<digest>' \\
  --build-arg AXIOM_GOLANG_BASE_IMAGE='golang:1.24-bookworm@sha256:<digest>' \\
  --build-arg AXIOM_PYTHON_BASE_IMAGE='python:3.12-slim-bookworm@sha256:<digest>' \\
  -t ghcr.io/notstarr/axiom:local --load .
```

不能使用未解析的 `<digest>` 占位符直接构建；发布 CI 会自动解析官方基础镜像 digest，在 `linux/amd64` 上下载并验证 Pi runtime，运行 Python/Ruff/compileall、前端检查、工具版本检查、Pi JSON/RPC/Extension smoke，并生成 SBOM/Provenance 后推送 GHCR。

发布所需的仓库 Secret：

```text
AXIOM_PI_RUNTIME_URL
AXIOM_PI_RUNTIME_SHA256
AXIOM_PI_RUNTIME_TOKEN      # 私有制品可选 Bearer Token
```

构建没有真实 Linux Pi runtime 时必须失败，不会发布一个只能启动 API、但 Worker 无法工作的镜像。

## 备份和恢复

```bash
axiomctl backup
```

会执行：

```text
pg_dump --format=custom
pg_dumpall --globals-only
```

输出到安装目录 `backups/`，包括数据库 dump、全局角色 SQL 和 SHA-256 manifest，默认文件权限为 `0600`。备份包含明文 API Key、Credential、Worker/MCP 配置；请将整个备份目录按现有主机权限保护。

恢复时不要覆盖正在运行的生产 Volume。先启动隔离 PostgreSQL 实例，使用 `pg_restore --exit-on-error` 恢复，再用 `axiomctl doctor` 和 API health 检查验证。生产解除 frozen 并产生新写入后不能回到 SQLite；只能恢复 PostgreSQL 备份或修复前进。

## 故障排查

```bash
axiomctl doctor
axiomctl status
axiomctl logs postgres
axiomctl logs migrator
axiomctl logs api
axiomctl logs runner
```

- **Docker 未安装/无权限**：安装 Docker Engine 和 Compose Plugin，确认当前用户能执行 `docker info`；不要在安装脚本中自动执行不可审查的系统包安装。
- **端口冲突**：设置 `AXIOM_PORT` 后重新运行安装或修改 `/opt/axiom/.env`，再执行 `axiomctl restart`。
- **迁移器失败**：先看 `axiomctl logs migrator`；修复数据库或镜像后重新执行 `axiomctl start`。不要手工跳过迁移。
- **API 不健康**：看 API 日志、`axiomctl status` 和迁移 revision；API 只接受当前 PostgreSQL Head。
- **Runner 不健康**：执行 `axiomctl doctor`，重点看 Pi/Node runtime、Extension smoke 和安全工具链版本。
- **MCP/Reqable 连接失败**：检查 MCP 地址是否能从容器访问；宿主机服务不要填 `127.0.0.1`，优先使用 `host.docker.internal` 或宿主机局域网地址，并检查 Reqable 监听、TLS、证书和防火墙。
- **磁盘不足**：检查 `docker system df`、`docker volume ls` 和 `df -h`；不要在未备份时删除 `axiom-data` 或 `axiom-postgres`。
- **更新失败**：保留 `axiomctl logs` 输出和 `.env.previous`，确认迁移是否可逆，再决定 `axiomctl rollback`；该命令不会回滚数据库。

## 验收边界

本机 macOS arm64 只能完成脚本契约、Python/前端静态检查和当前开发 runtime smoke。以下必须在全新 Ubuntu 22.04/24.04 Linux x86_64 主机实测：

- Docker buildx `linux/amd64` 镜像构建；
- 真实 Pi 0.82.1 JSON/RPC 和 AXIOM Extension 加载；
- Chromium headless 启动和 Browser Broker；
- 空 PostgreSQL Compose 初始化、迁移器、API/Runner 容器间链路；
- 一行 `curl | bash` 安装、幂等重跑、更新、备份、恢复、卸载和 `--purge`；
- 无 MCP 与外部 MCP/Reqable 连接；
- 创建任务、四阶段 Worker/Reviewer Lease 和 Artifact/Workspace 共享。

## 发布清单、更新状态与回滚边界

每个 AXIOM 应用镜像都包含：

```text
/opt/axiom/release/manifest.json
/opt/axiom/release/SHA256SUMS
```

`manifest.json` 是更新前的机器可读验收清单，至少记录 AXIOM 版本、Git revision、构建时间、Linux 架构、Pi/Node runtime、全部内置 CLI 工具版本，以及：

```json
{
  "database": {
    "migration_target": "postgresql@head",
    "rollback_compatible": false
  }
}
```

`database.rollback_compatible` 必须是 JSON 布尔值。默认值为 `false`，只有经过发布验证确认新版本迁移与旧应用兼容时才可以设为 `true`。`SHA256SUMS` 保存 Pi、Node 和 release manifest 的 SHA-256，`axiomctl doctor` 会检查 Runner 中的清单和 runtime。

`axiomctl update` 会把更新前的配置保存为：

```text
/opt/axiom/.env.previous
/opt/axiom/.axiom-update-state
```

状态文件记录候选镜像、目标版本、迁移是否成功和回滚兼容标记。迁移成功后，如果新 API/Runner 启动失败，控制脚本不会悄悄切回旧镜像，而是保留数据库和候选版本，提示查看日志并 **fix-forward**。这样不会让已经升级的 Schema 被旧应用误写。

`axiomctl rollback` 只恢复应用镜像和 `.env`，**永远不执行 Alembic downgrade**。当状态文件中的 `ROLLBACK_COMPATIBLE=0` 且迁移已经成功时，回滚会被拒绝；只有兼容版本允许执行应用回退。若回退后的容器健康检查失败，脚本会恢复磁盘上的活动 `.env` 并尽力按该配置重新创建 API/Runner，避免配置和运行容器不一致。

## CI 与 Linux 实机验收边界

CI 发布流程固定使用 `linux/amd64`，构建前必须注入真实 Linux x86_64 Pi runtime；缺少 `runtime/manifest.json`、Pi/Node 哈希不匹配、Extension smoke 失败或任一安全工具版本命令失败时，镜像构建直接失败。CI 会执行 Python、Ruff、compileall、前端 JavaScript、Compose 配置、Pi JSON/RPC/Extension smoke，并为镜像生成 SBOM/Provenance。

当前 macOS arm64 工作区只能验证脚本契约、Python 测试和静态检查，不能替代以下验收：

- Ubuntu 22.04/24.04 x86_64 上的真实 `curl | bash` 安装和幂等重跑；
- 空 PostgreSQL Volume 初始化、`postgresql@head` 迁移和 API/Runner 容器链路；
- Linux Chromium headless、Browser Broker、Pi JSON/RPC 和四阶段 Worker 生命周期；
- 无 MCP 与外部 MCP/Reqable 连接、备份恢复、update/rollback 实机流程。

必须在 Linux 实机确认后才可将 release tag 和镜像 digest 作为正式安装入口。
