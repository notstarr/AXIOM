# AXIOM PostgreSQL 运维手册

> 本文适用于当前宿主机 API/Runner + 本机 Docker PostgreSQL 架构。所有示例都避免打印数据库密码；生产环境文件包含明文连接信息，必须保持 `0600`。

## 1. 路径和服务

```text
仓库：       /Users/hexin/vscode/xrayAI
AXIOM：      /Users/hexin/vscode/xrayAI/axiom
数据目录：   /Users/hexin/.local/share/axiom-v3
环境文件：   /Users/hexin/.local/share/axiom-v3/postgresql.env
API：        http://127.0.0.1:8888
PostgreSQL： 127.0.0.1:55432/axiom
Schema：     axiom
```

LaunchAgent：

```text
com.xrayai.axiom
com.xrayai.axiom.runner
com.xrayai.axiom.postgresql-backup
com.xrayai.axiom.postgresql-observer   # 切换后观察期
```

## 2. 环境文件

模板：

```text
/Users/hexin/vscode/xrayAI/axiom/.env.postgresql.example
```

生产文件：

```bash
chmod 600 /Users/hexin/.local/share/axiom-v3/postgresql.env
```

允许保存明文密码和 URL，但禁止：

- 提交到 Git；
- 粘贴到工单、聊天或普通日志；
- 在 `ps` 参数中直接写生产密码；
- 修改 LaunchAgent 重新内嵌 URL/密码。

LaunchAgent 只保存 `AXIOM_PG_ENV_FILE` 路径，应用进程启动时读取该文件。

## 3. 启动、停止和状态

从仓库根目录执行：

```bash
cd /Users/hexin/vscode/xrayAI
./start.sh start
./start.sh status
./start.sh stop
```

`start` 会：

1. `uv sync --frozen`；
2. 启动 PostgreSQL Compose 服务；
3. 等待 `pg_isready`；
4. 使用 migrator 执行 `postgresql@head`；
5. 将维护状态设为 `normal`；
6. 重建 API/Runner LaunchAgent；
7. 等待 `/health` 返回 PostgreSQL backend。

`stop` 只停止 API 和 Runner，PostgreSQL 保持在线。

应用代码与迁移：

```bash
cd /Users/hexin/vscode/xrayAI
./apply.sh all
```

可选目标为 `all|server|runner`。脚本会构建需要更新的静态资源、应用 PostgreSQL Alembic 分支并重启对应进程。

## 4. 健康检查

API：

```bash
curl -fsS http://127.0.0.1:8888/health | python3 -m json.tool
```

应包含：

```text
status = ok
database.backend = postgresql
database.migration = 20260805_pg0001（或未来 postgresql@head）
maintenance.state = normal
```

整体状态：

```bash
cd /Users/hexin/vscode/xrayAI
./start.sh status
```

LaunchAgent：

```bash
launchctl print gui/$(id -u)/com.xrayai.axiom
launchctl print gui/$(id -u)/com.xrayai.axiom.runner
launchctl print gui/$(id -u)/com.xrayai.axiom.postgresql-backup
```

PostgreSQL 容器：

```bash
cd /Users/hexin/vscode/xrayAI/axiom
docker compose -f compose.yaml -f compose.host.yaml ps
docker compose -f compose.yaml -f compose.host.yaml logs --tail=200 postgres
```

## 5. Schema 升级

正常部署使用：

```bash
cd /Users/hexin/vscode/xrayAI/axiom
source /Users/hexin/.local/share/axiom-v3/postgresql.env
.venv/bin/axiom db upgrade --database-url "$AXIOM_MIGRATOR_DATABASE_URL"
```

规则：

- 生产只允许 PostgreSQL 分支 `postgresql@head`；
- 不得在 PostgreSQL 上执行历史 SQLite revision；
- 不得修改已经冻结的 35 个 SQLite 迁移；
- API 不会替你偷偷升级 Schema，版本不匹配时会拒绝启动。

## 6. 维护状态

维护状态存放在共享数据目录，不依赖数据库，因此数据库切换期间也能阻止写入。

查看：

```bash
/Users/hexin/vscode/xrayAI/axiom/.venv/bin/axiom maintenance status \
  --data-dir /Users/hexin/.local/share/axiom-v3
```

进入 drain：

```bash
/Users/hexin/vscode/xrayAI/axiom/.venv/bin/axiom maintenance set drain \
  --data-dir /Users/hexin/.local/share/axiom-v3 \
  --actor human.ops --reason "planned maintenance"
```

进入 frozen：

```bash
/Users/hexin/vscode/xrayAI/axiom/.venv/bin/axiom maintenance set frozen \
  --data-dir /Users/hexin/.local/share/axiom-v3 \
  --actor human.ops --reason "database validation"
```

恢复：

```bash
/Users/hexin/vscode/xrayAI/axiom/.venv/bin/axiom maintenance set normal \
  --data-dir /Users/hexin/.local/share/axiom-v3 \
  --actor human.ops --reason "maintenance complete"
```

语义：

| 状态 | 新 Lease | 已有 Lease 心跳/收口 | 其它 HTTP 写入 |
|---|---:|---:|---:|
| `normal` | 允许 | 允许 | 允许 |
| `drain` | 禁止 | 允许 | 允许，但创建 Task、Resume、Continuation、Reason Trigger、Review 和漏洞等“启动新工作”入口被拒绝 |
| `frozen` | 禁止 | 禁止业务写入 | 拒绝 |

不要在活动 Lease 尚未清空时直接停止数据库。计划维护先进入 `drain`，确认活动工作为 0，再进入 `frozen`。

## 7. 备份

手工备份：

```bash
cd /Users/hexin/vscode/xrayAI/axiom
./ops/postgres/backup.sh
```

输出三条路径：

1. custom-format database dump；
2. globals SQL；
3. SHA-256 manifest。

默认目录：

```text
/Users/hexin/.local/share/axiom-v3/postgresql-backups/daily
/Users/hexin/.local/share/axiom-v3/postgresql-backups/weekly
```

保留 14 份每日和 8 份每周备份。备份包含明文 Credential/API Key，文件权限为 `0600`。

安装或刷新每日备份 LaunchAgent：

```bash
cd /Users/hexin/vscode/xrayAI/axiom
./ops/postgres/install_backup_launchagent.sh
```

## 8. 恢复验证

不要直接覆盖生产数据库来“试一下备份”。使用临时数据库恢复验证：

```bash
cd /Users/hexin/vscode/xrayAI/axiom
./ops/postgres/restore_verify.sh \
  /Users/hexin/.local/share/axiom-v3/postgresql-backups/daily/axiom-<timestamp>.dump
```

如果有源 fingerprint：

```bash
./ops/postgres/restore_verify.sh \
  /path/to/axiom-<timestamp>.dump \
  /path/to/expected-database-fingerprint.json
```

恢复器会在临时数据库中执行 `pg_restore --exit-on-error`、计算 fingerprint、启动 frozen API、导出快照，完成后自动删除临时数据库。报告默认写入：

```text
/Users/hexin/.local/share/axiom-v3/postgresql-migration/restore-<timestamp>
```

每月至少执行一次恢复验证。数据库升级或大规模数据操作后应立即额外执行一次。

## 9. 观察和锁诊断

切换后观察器：

```bash
launchctl print gui/$(id -u)/com.xrayai.axiom.postgresql-observer
tail -n 20 /Users/hexin/.local/share/axiom-v3/postgresql-migration/\
cutover-20260805T183453Z/observation-24h/samples.jsonl
```

观察完成后读取：

```text
.../observation-24h/summary.json
```

只有 `passed=true` 才能关闭 24 小时观察验收。

需要数据库级诊断时，在 Compose 容器中使用管理员连接，避免把 URL 暴露到命令历史：

```bash
cd /Users/hexin/vscode/xrayAI/axiom
docker compose -f compose.yaml -f compose.host.yaml exec -T postgres \
  psql -U axiom_bootstrap -d axiom -c \
  "select pid, application_name, state, wait_event_type, wait_event from pg_stat_activity order by pid"
```

等待锁：

```bash
docker compose -f compose.yaml -f compose.host.yaml exec -T postgres \
  psql -U axiom_bootstrap -d axiom -c \
  "select pid, locktype, mode, granted from pg_locks where not granted"
```

不要在不了解事务归属时手工终止连接。优先停止相关 API/Runner、保留日志和 `pg_stat_activity` 快照，再决定是否 `pg_terminate_backend`。

## 10. 测试数据库

测试 PostgreSQL 使用独立 Compose、端口和数据库：

```bash
cd /Users/hexin/vscode/xrayAI/axiom
docker compose -f compose.test.yaml up -d postgres
.venv/bin/pytest -q
```

默认测试连接：

```text
127.0.0.1:55433/axiom_test
```

普通测试不允许回退 SQLite。只有 SQLite 历史导入器测试可以打开固定 `20260805_0035` fixture。

## 11. SQLite 历史导入

仅对冻结历史源使用：

```bash
.venv/bin/axiom db migrate-sqlite \
  --sqlite-path /path/to/frozen.db \
  --database-url "$AXIOM_DATABASE_URL" \
  --data-dir /path/to/source-data \
  --report-dir /path/to/report
```

目标必须是已升级且为空的 PostgreSQL 数据库。导入器不是“增量同步”或“再次补数据”工具，不能对生产非空库运行。

## 12. 故障处理边界

### API 不可用、PostgreSQL健康

1. 检查 `server.log` 和 `/health`；
2. 检查 Alembic revision 是否匹配；
3. 使用 `./apply.sh server` 重建 API；
4. Runner 可保持运行，控制面恢复后会继续 reconciliation。

### PostgreSQL不可用

1. 进入 `frozen`（若 API 仍可执行维护命令则先 frozen）；
2. 停止 API/Runner；
3. 检查 Docker、磁盘、容器日志和数据卷；
4. 不能确认数据卷安全时，不要反复重建容器；
5. 必要时从最近通过校验的 PostgreSQL 备份恢复到新实例。

### 迁移版本不匹配

1. 不绕过 API 启动检查；
2. 确认运行代码 commit 与目标 Alembic Head；
3. 使用 `axiom_migrator` 执行 `axiom db upgrade`；
4. 迁移失败时保留日志和数据库快照，先在副本复现。

### 回滚

生产已经开放 PostgreSQL 写入，**禁止回退 SQLite**。可以回滚应用代码，但前提是该代码支持当前 PostgreSQL Schema；数据库故障只能从 PostgreSQL 备份恢复或修复前进。

## 13. 发布前后检查表

发布前：

- [ ] `git status` 没有误包含生产数据、环境文件或测试产物；
- [ ] PostgreSQL测试容器健康；
- [ ] 全量 pytest；
- [ ] Ruff、compileall、全部非 vendor JavaScript `node --check`；
- [ ] Alembic upgrade/downgrade/re-upgrade（涉及迁移时）；
- [ ] `git diff --check`；
- [ ] 生产活动 Lease 和维护窗口已确认。

发布后：

- [ ] `/health` 为 PostgreSQL且 revision 正确；
- [ ] API/Runner LaunchAgent running；
- [ ] 真实 claim/heartbeat/complete 无重复领取；
- [ ] Dashboard、Task、图谱、漏洞、归档、文件、Worker、Skill、MCP 正常；
- [ ] 手工备份成功；
- [ ] 恢复验证成功；
- [ ] 观察器持续写样本且无锁/重复工作异常。
