# AXIOM 当前生产架构

> 状态：已核对至 2026-07-27。本文描述当前实际代码，不描述未落地的 ASM、容器化或爬虫方案。

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
 ├─ reason         ReasonRun、触发策略、Workspace 和规划提交
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
  → bootstrap       i001 完成并生成 Fact 后创建初始 ReasonRun
  → reason          独立 ReasonRun 被空闲 Reason Worker 领取
  → explore         pending Explore Intent 由空闲 Worker 自动领取
  → reason          新 Fact、失败终态、可执行 Todo、低水位或最大等待触发
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
→ POST Reason Trigger Reconciler
→ GET dispatch/snapshot
→ Reconciler 从统一可领取工作视图计算 claim actions
→ WorkerAllocator 按 capability、Task worker_pool、disabled、cooldown、max_running 选择
→ API compare-and-swap claim
→ ThreadPoolExecutor 异步执行
```

统一工作视图同时包含 Bootstrap/Explore Intent 和 ReasonRun，Task 内优先级为 `Bootstrap → Reason → Explore`；Task 之间按最近一次领取时间轮询，每轮优先给每个 Task 一个工作，避免低 ID Task 长期占满槽位。ReasonRun 占用全局和单 Task Run 槽位，但不计入 Intent 数量或 Explore 并发。

有效领取会同时校验 `global_max_active_runs`、`global_max_active_tasks`、`per_task_max_active_runs`、`per_task_explore_concurrency`、Worker `max_running`、能力和 Task Worker Pool。纯调度计算先过滤一次，数据库 claim 事务再校验一次，防止多个 tick/Runner 并发越限。Bootstrap 和 Reason 的单 Task 并发固定为 1。

Reason 提交后只创建 `pending` Explore Intent，不指定具体 Worker。空闲且兼容的 Worker 会自动领取；容量不足保持 pending，不记失败，也不要求 Reason 重新规划。

Intent 和 ReasonRun 都使用独立 `lease_id`。领取后先启动心跳，再初始化 Context、运行目录和 Pi；执行中持续 heartbeat。执行重试和基础设施重试分开计数，超出预算才进入终态并成为下一轮 Reason 的输入信号。

Runner 对 API 暂时不可用捕获 `ControlPlaneError`，保留进程并按下一轮间隔重试。API 重启演练已验证 Runner PID 不变、API 恢复后继续调度。

## 5. 三个阶段

当前流程保留 Bootstrap，不使用 ASM 替代：

```text
Bootstrap → ReasonRun → Explore → ReasonRun → Explore → ... → Goal
```

### Bootstrap

只做广度优先攻击面初始化，产出：

```text
{run_dir}/asset_inventory.md
{run_dir}/initial_leads.md
{run_dir}/findings.md
```

Worker 最后一条 assistant 文本必须精确等于 `FINISH`。Runner 随后校验三个固定文件均为运行目录内的非空普通文件，拒绝 symlink 和越界路径，将内容纳入正式 FileStore，由代码计算 hash、size、type 并幂等注册 Artifact，再生成包含三个 Artifact ID 的 Bootstrap Fact。模型不再编写 Artifact 数组或 Fact。

`bootstrap_artifacts.json` 仍可出现在运行目录中，但它是代码在成功提交后生成的内部索引，不是 Worker 输入协议，也不会注册为 Artifact。

### Reason

Reason 不再伪装成 `kind=reason` Intent，而是独立的 `ReasonRun`。状态机为：

```text
pending → claimed → running → concluding → completed
                    └──────────────→ failed / cancelled
```

`reason_run_sources` 记录本轮消费的 Fact；同一 Task 通过数据库唯一约束和 Service 双重保证最多一个未结束 ReasonRun。Reason Workspace 以 `reason_run_id` 关联当前运行，历史 Workspace 的旧 `intent_id` 只保留只读兼容。

Runner 注入 `AXIOM_REASON_RUN_ID`、`AXIOM_REASON_WORKSPACE_ID`、ReasonRun lease、API 地址和服务令牌；新 Reason 不需要 `AXIOM_INTENT_ID`。Worker 只能通过 Axiom CLI 经 HTTP 增量操作 Workspace，不访问 SQLite，也不编写大型计划 JSON：

```text
axiom reason status
axiom reason todo add
axiom reason todo update
axiom reason intent propose
axiom reason goal assess
```

Runner 会把当前 AXIOM 虚拟环境的 `bin` 目录放到 Pi 子进程 `PATH` 首位。这里必须使用 `Path(sys.executable).parent`，不能先 `resolve()`；虚拟环境 Python 是符号链接，提前解析会落到系统 Python 目录并再次造成 `axiom: command not found`。

每个命令只提交一个小操作并立即校验、持久化。Reason 执行期间 Todo 更新和 Intent proposal 只存在于 Workspace，正式 Intent 表和图谱不变化。每个 Intent 的 `execute_seconds` / `conclude_seconds` 是结构化字段，不写入 description；Reason 不选择 Worker，也没有漏洞上报入口。

每轮新增 Intent 由后端强制限额：

```text
remaining = min(
  reason_max_new_intents_per_run,
  reason_target_open_intents - 当前开放 Explore 数
) - 当前 Workspace proposal 数
```

开放 Explore 包括 `pending/claimed/running/concluding`。额度耗尽时 CLI 立即返回当前开放数、目标数和剩余额度；commit 在同一事务中再次校验。Todo 数量不与 Intent 额度绑定。

Reason 最后一条 assistant 文本精确等于 `FINISH` 后，Runner 先读取 Workspace readiness：必须存在 Goal assessment，并且要么继续派发工作，要么用真实 Fact 证据证明 Goal 已达成。校验通过才调用 `/commit`。提交后由代码生成 `reason_plan.json` 审计文件；`/submit` 不再存在。

同一轮新增 Todo 可以使用临时 `ref` 被 Intent 或 Goal Assessment 引用。提交事务先创建 Todo，再将临时引用解析为真实 `tdXXX`；引用不存在时整批回滚，不会留下半套规划或错误的 Goal Evaluation。

Reason Trigger Reconciler 在 Bootstrap/Explore 完成后立即评估，并在每个 Runner tick 周期评估。前置条件是 Task running、Bootstrap 已完成、Goal 未达成、没有活动 ReasonRun，且除初次外已满足最短间隔。满足以下任一条件时创建新 ReasonRun：未规划 Fact 达到批量数；开放 Explore 低于低水位且存在新 Fact、失败终态或未派发的可执行 Todo；最老未规划 Fact 达到最大等待。输入状态指纹防止同一低水位状态空转，Task 恢复只重新评估规则，不无条件触发。

输入指纹去重同时覆盖 `completed` 和重试耗尽后的 `failed` ReasonRun，避免相同终态输入在每个 tick 被无限重建；人工停止产生的 `cancelled` 不参与去重，因此恢复 Task 后仍可按当前事实重新评估。

Goal 未达成时，Workspace 必须已有开放 Explore 或至少提交一个 proposal；Goal 达成时禁止 proposal，并要求至少一个真实 `fact:<id>` 证据。完成事务创建 Goal Evaluation、取消其它开放 Intent/ReasonRun 并结束 Task。新图谱不创建 Reason 节点，最新 Goal 证据直接显示为 `Fact(s) → Goal`；历史 Reason Intent 保持只读原样。

Task stop、人工/Reason Goal 完成、ReasonRun release 和 stale recovery 都会在同一事务中把对应 `open` Workspace 标记为 `abandoned`，旧 lease 随即失效。当前正在完成 Goal 的 Workspace 会被排除在批量废弃之外，并在事务尾部正常标记为 `committed`，避免出现终态 Task 下仍可写入的僵尸 Workspace。

### Explore

Explore 执行一个明确 Intent，可以为完成当前 Intent 寻找必要依赖，但不能越过 Task Scope。产出：

```text
{run_dir}/artifacts/**
```

Worker 最后一条 assistant 文本必须精确等于 `FINISH`，且 `artifacts/` 中至少有一个非空普通文件。Runner 只遍历该专用目录，拒绝 symlink、越界路径、隐藏/危险路径，以及 `sessions/`、`process/`、`models.json`；不会扫描整个运行目录。

只有证据充分时，Explore 使用 `axiom explore vulnerability report` 逐条上报 Vulnerability。命令按现有 `VulnerabilityReport` 字段校验，`source`、`reported_by`、`intent_id` 由执行环境补齐。漏洞先写入代码维护的 lease 级 Explore 状态，阶段完成时与 Artifact、Fact 在服务端收口；Reason 不上报漏洞。

Explore Fact 由代码使用原 Intent description、Artifact ID 和本轮 Vulnerability ID 生成，不使用模型尾部文本，也不会退化成无信息的统一句子。`explore_result.json` 只是代码维护的内部状态/索引，不要求 Worker 编写，也不注册为 Artifact。

### 统一完成协议

三个阶段都只认最后一条 assistant 文本去除首尾空白后精确等于：

```text
FINISH
```

工具输出、代码块、`FINISH` 前后附加说明或大小写变化均不算完成。`FINISH` 只触发产物/Workspace 校验，不直接改状态；缺失项会在同一 Pi 会话和共享 conclude deadline 内最多收口 3 次，明确告诉 Worker 只补文件或 Workspace 操作，不重新执行网络探索。

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

阶段文件采用 prepare → 文件提交 → 数据库事务 → finish 的收口顺序。数据库事务成功后删除 Worker 运行目录中的原文件，正式 Artifact 内容只保留一份；事务失败则回滚暂存文件并保留运行目录证据供同 lease 收口重试。内部索引、Pi session、进程日志和模型配置永远不会被自动注册为 Artifact。

项目凭证、Task Hints、Project Knowledge 分别保存；凭证按阶段执行输入按需提供，不混入 Origin，也不写入 Fact、Artifact 摘要或命令参数。

## 8. 前端

前端继续使用原有信息架构和图谱视觉，通过 V2 API 适配层对接模块化后端。当前主要页面：Dashboard、任务管理、任务新建、概览、可切换图谱、Todo、漏洞、文件、MCP、Worker、Skill、配置、Guard、项目。

任务详情的“概览”和“图谱”是两种人类视图：图谱不是执行事实源；右侧 Todo、事实、产物和漏洞分别从 V2 API 读取。ReasonRun 只显示为图谱上方的运行活动卡片，可查看 Worker、状态、当前活动、工具、心跳、触发器、预算和配置快照，不创建图谱节点。

设置页只编辑真实 `RuntimeConfig`，分为“调度并发、阶段预算、Reason 触发与派发、高级可靠性”四组；Bootstrap/Reason 单 Task 并发以只读 `1（固定）` 展示。保存使用 `expected_version` 乐观锁，合法零值不会被 `||` 回填；Runner 每 tick 读取版本并回报真实 `applied_version/applied_at`。生产浏览器已验证保存、刷新、重启持久化和下一 tick 热应用。

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

ReasonRun/RuntimeConfig 迁移版本为 `20260727_0020`。迁移保留历史 Reason Intent、Workspace 和 Goal Evaluation，不重写历史图谱；新运行全部使用 `reason_run_id`。最终重启前一致性备份位于 `/Users/hexin/.local/share/axiom-v3/backups/20260727-131804-reasonrun-final/`。空库和包含真实 ReasonRun 数据的生产副本都完成 `upgrade → downgrade 0019 → upgrade 0020` 演练；回滚时 ReasonRun 会物化为旧版只读 Reason Intent，并同步 Workspace、Context、Goal Evaluation、Audit 和来源 Fact。SQLite integrity 为 `ok`，foreign key violation 为 0。

## 10. 当前明确不属于主流程的内容

- Bootstrap 没有改成 ASM；ASM 工具链不是当前阶段。
- 旧爬虫后端逻辑不作为 V2 新任务入口，前端不展示。
- 每任务容器、VPN/SOCKS5 强制出口、MCP Bridge 是后续方案，不影响当前宿主机 Runner。
- `cdn.tailwindcss.com` 仍产生浏览器生产警告，但不影响业务功能；后续可替换为构建产物。

## 11. 验证基线

```text
uv run pytest -q                         163 passed
uv run ruff check .                      passed
python -m compileall -q src tests        passed
node --check <all static js>             passed
bash -n start.sh restart.sh              passed
```

以上测试已覆盖 FINISH 精确识别、同会话有界收口、阶段文件安全、Artifact/Fact 自动生成、ReasonRun 唯一性/lease/心跳/回收、Workspace 增量操作和派发额度、Reason 触发规则、全局/Task/阶段并发、公平领取、RuntimeConfig 版本冲突与热应用、Guard 真实配置，以及停止/Goal 完成后的立即取消；仅有 Starlette/httpx 弃用警告。

2026-07-27 生产受控 E2E `t_138` 完成 `Bootstrap → ReasonRun(rr000001) → Explore → ReasonRun(rr000002) → Goal`：正式图谱只有 Bootstrap/Explore 两个 Intent，两个 ReasonRun 均零重试完成；生成 4 个 Artifact、1 个 completed Todo 和两个代码生成 Fact，最终 `goal_from_fact_ids=[f002]`。Task completed 后开放 Intent、ReasonRun、Workspace 均为 0。

最终重启后又执行 API-only 临时验收 `t_141`（随后删除）：Bootstrap 完成后创建独立 ReasonRun，图谱仍只有 Bootstrap Intent；打开 Workspace 后停止 Task，ReasonRun 立即 `cancelled`、Workspace 变为 `abandoned(task_stopped)`、旧 heartbeat 和 commit 均返回 409。临时 Task/Project/Worker 已清理，生产 RuntimeConfig 保存/应用版本均为 v210，活动 Intent、ReasonRun、open Workspace 均为 0。浏览器再次确认 `t_138` 图谱只有 2 个正式 Intent，设置页四组配置和 `Runner 已应用 v210` 正常显示；控制台仅保留既知 Tailwind CDN 警告。
