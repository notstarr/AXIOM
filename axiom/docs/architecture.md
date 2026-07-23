# AXIOM 当前生产架构

> 状态：已核对并在 2026-07-23 完成生产切换验证。本文描述当前实际代码，不描述未落地的 ASM、容器化或爬虫方案。

## 1. 运行边界

AXIOM 是一个模块化单体，由两个长期进程组成：

```text
axiom serve       HTTP API、领域服务、SQLite 持久化、前端静态文件
axiom runner      API-only 调度器、Worker 领取、阶段执行、租约心跳
```

Runner 不直接打开数据库，只通过 `/api/v1/dispatch/*` 和执行接口访问 API。API/SQLite 是唯一事实源；Worker 是由 Runner 启动的 Pi 子进程，不是数据库客户端。

生产运行目录：

```text
代码：      /Users/hexin/vscode/xrayAI/axiom
API：       http://127.0.0.1:8888
数据库：    /Users/hexin/.local/share/axiom-v3/axiom.db
数据文件：  /Users/hexin/.local/share/axiom-v3
旧运行时：  /Users/hexin/.local/share/axiom
```

macOS LaunchAgent：

```text
com.xrayai.axiom
com.xrayai.axiom.runner
```

## 2. 模块边界

每个模块拥有自己的 schema、repository、service、API route 和测试；跨模块流程通过明确的 Service 和事务协调。

```text
API Router
 ├─ projects       项目
 ├─ tasks          任务、Scope、Hints、生命周期
 ├─ graph          Fact / Intent 图谱
 ├─ todos          单层共享 Todo
 ├─ reason         Reason Workspace 和规划提交
 ├─ context        Manifest、索引、按 ref 展开
 ├─ execution      claim 后的 start/input/heartbeat/complete/release
 ├─ dispatch       Snapshot、恢复、领取
 ├─ artifacts      文件产物元数据与内容
 ├─ vulnerabilities 漏洞对象、状态、导出
 ├─ external_reports 外部上报与重试记录
 ├─ credentials    项目凭证
 ├─ workers        Worker 能力、并发和 Pi 配置
 ├─ settings       Runtime / Reason 触发配置
 ├─ mcp            Server 导入、发现、工具元数据和启停
 ├─ skills         共享 Skill 目录管理
 ├─ guard          监管决策和审计
 ├─ goals          Goal Evaluation
 ├─ audit          追加式审计
 ├─ diagnostics    任务健康、Worker、失败和 Token 统计
 └─ exports        任务、漏洞和报告导出
```

## 3. 任务状态机

新任务创建事务会同时写入 Project 关联、Task、Task Hints、`origin`/`goal` Fact 和 Bootstrap `i001` Intent。

```text
running
  → bootstrap       i001 完成后进入 Reason
  → reason          Reason 持有任务级租约并提交 Todo/Intent
  → explore         pending Explore Intent 由空闲 Worker 自动领取
  → reason          新 Fact、终态 Intent、重试耗尽或低水位触发
  → completed       只有 Goal Evaluation 达成才进入

任何运行态
  → stopped         人工停止；Runner 取消本地 Pi/RPC 进程
stopped
  → running         人工继续；按当前阶段重新进入调度
```

Task 完成的唯一业务指标是 Goal Evaluation。没有 Intent、没有 Worker 或某一轮 Reason 结束都不能单独表示任务完成。

## 4. 调度和领取

一次 Runner tick：

```text
读取 Runtime Settings
→ recover-stale
→ 清理已完成 Future
→ GET dispatch/snapshot
→ Reconciler 计算 claim actions
→ WorkerAllocator 按 capability、Task worker_pool、disabled、cooldown、max_running 选择
→ API compare-and-swap claim
→ ThreadPoolExecutor 异步执行
```

Reason 只创建 `pending` Intent，不指定具体 Worker。空闲且兼容的 Worker 会自动领取；容量不足保持 pending，不记失败，也不要求 Reason 重新规划。

Intent 使用独立 `lease_id`。领取后先启动心跳，再初始化 Context、运行目录和 Pi；执行中持续 heartbeat。执行重试和基础设施重试分开计数，超出预算才进入 `retry_exhausted` 并触发 Reason 重规划。

Runner 对 API 暂时不可用捕获 `ControlPlaneError`，保留进程并按下一轮间隔重试。API 重启演练已验证 Runner PID 不变、API 恢复后继续调度。

## 5. 三个阶段

当前流程保留 Bootstrap，不使用 ASM 替代：

```text
Bootstrap → Reason → Explore → Reason → Explore → ... → Goal
```

### Bootstrap

只做广度优先攻击面初始化，产出：

```text
{run_dir}/bootstrap_artifacts.json
```

其中包含中文 Markdown：`asset_inventory`、`initial_leads`、`findings`。Runner 校验并原子注册 Artifact，再生成 Bootstrap Fact；Worker 不直接修改图谱。

### Reason

Reason 使用任务级独占 Workspace。它读取渐进式 Context，更新单层 Todo，并提交 Intent proposal。每个 Intent 的 `execute_seconds` / `conclude_seconds` 是结构化字段，不写入 description；Reason 不选 Worker。

同一轮新增 Todo 可以使用临时 `ref` 被 Intent 或 Goal Assessment 引用。提交事务先创建 Todo，再将临时引用解析为真实 `tdXXX`；引用不存在时整批回滚，不会留下半套规划或错误的 Goal Evaluation。

触发信号由 Snapshot/Reason policy 计算，包含初始规划、新 Fact、终态 Intent 增量、未完成 Intent 低水位、最长等待和重试耗尽；任务级 Reason lease 防止并发规划。

### Explore

Explore 执行一个明确 Intent，可以为完成当前 Intent 寻找必要依赖，但不能越过 Task Scope。产出：

```text
{run_dir}/explore_result.json
```

结果必须是中文 Fact 摘要、至少一个 Artifact 和可复现证据；只有证据充分时才上报 Vulnerability。JSON 无效会在共享 deadline 内进行有限修复/收口重试，不能用半截文本直接写 Fact。

## 6. 渐进式披露 Context

ContextService 为每次阶段执行创建 revision：

```text
contexts/{task_id}/r000001/
 ├─ manifest.json
 ├─ graph.json
 ├─ todos.json
 ├─ artifacts.json
 ├─ hints.json
 ├─ vulnerabilities.json
 ├─ knowledge.json
 └─ goal_evaluations.json
```

索引中的 Fact/Intent/Artifact/Todo 只保留 preview 和 detail ref；Worker 先读 Manifest 与索引，需要时通过 detail 文件或 Context expand API 展开。这样不会把整个图谱和长产物重复塞进 Reason/Explore 上下文。

## 7. 文件和产物

FileStore 使用外部 data-dir，Artifact 表只保存元数据、hash、size 和 storage key；文件内容仍进入现有文件管理体系。任务删除、Artifact 归档和迁移均不允许通过路径穿越访问外部文件。

项目凭证、Task Hints、Project Knowledge 分别保存；凭证按阶段执行输入按需提供，不混入 Origin，也不写入 Fact、Artifact 摘要或命令参数。

## 8. 前端

前端继续使用原有信息架构和图谱视觉，通过 V2 API 适配层对接模块化后端。当前主要页面：Dashboard、任务管理、任务新建、概览、可切换图谱、Todo、漏洞、文件、MCP、Worker、Skill、配置、Guard、项目。

任务详情的“概览”和“图谱”是两种人类视图：图谱不是执行事实源；右侧 Todo、事实、产物和漏洞分别从 V2 API 读取。生产浏览器验收已覆盖 Dashboard、任务列表、概览/图谱切换和 MCP 工具展示。

## 9. 迁移与回滚

```text
start.sh start
  停止旧 Cairn Server/Dispatcher
  SQLite backup 创建一致性源快照
  migrate-legacy 写入 axiom-v3
  db-upgrade
  启动新 API，等待 /health api:v1
  启动新 Runner

start.sh rollback
  停止新 API/Runner
  恢复保留的旧 LaunchAgent
  等待 /health api:v2
```

生产迁移报告位于：

```text
/Users/hexin/.local/share/axiom-v3/backups/<cutover>/migration-report.json
```

最终迁移验证：项目 9、任务 40、Fact 2135、Intent 2207、Todo 77、Artifact 205、漏洞 453、凭证 12、Worker 4、MCP Server 2、MCP 工具 63，SQLite integrity 和 foreign key check 均通过。旧 v2 回滚并恢复新 v1 的演练已成功。

## 10. 当前明确不属于主流程的内容

- Bootstrap 没有改成 ASM；ASM 工具链不是当前阶段。
- 旧爬虫后端逻辑不作为 V2 新任务入口，前端不展示。
- 每任务容器、VPN/SOCKS5 强制出口、MCP Bridge 是后续方案，不影响当前宿主机 Runner。
- `cdn.tailwindcss.com` 仍产生浏览器生产警告，但不影响业务功能；后续可替换为构建产物。

## 11. 验证基线

```text
uv run pytest -q                         94 passed
uv run ruff check .                      passed
python -m compileall -q src tests        passed
node --check <all static js>             passed
bash -n start.sh restart.sh              passed
```

以上测试在 Runner 断连恢复修复后重新执行；仅有 Starlette/httpx 弃用警告。
