# AXIOM 当前生产架构

> 状态：已核对至 2026-08-05。本文描述当前实际代码，不描述未落地的 ASM、容器化或爬虫方案。

## 1. 运行边界

AXIOM 是一个模块化单体，由两个长期进程组成：

```text
axiom serve       HTTP API、领域服务、PostgreSQL 持久化、前端静态文件
axiom runner      API-only 调度器、Worker 领取、阶段执行、租约心跳
```

Runner 不直接打开数据库，只通过 `/api/v1/dispatch/*` 和执行接口访问 API。API/PostgreSQL 是唯一事实源；Worker 是由 Runner 启动的 Pi 子进程，不是数据库客户端。SQLite 已退出生产、开发和普通测试运行时，只作为冻结历史源供一次性只读导入器读取。

生产运行目录：

```text
代码：      /Users/hexin/vscode/xrayAI/axiom
API：       http://127.0.0.1:8888
PostgreSQL：127.0.0.1:55432/axiom（Schema: axiom）
数据库配置：/Users/hexin/.local/share/axiom-v3/postgresql.env（0600）
数据文件：  /Users/hexin/.local/share/axiom-v3
SQLite：    只读最终备份和历史迁移输入，不再提供运行时
```

PostgreSQL 18.4 运行在本机 Docker，API 与 Runner 继续运行在宿主机。镜像使用固定 digest；业务对象由不可登录的 `axiom_owner` 持有，Alembic 使用 `axiom_migrator`，API/Runner 使用 `axiom_app`。`public` Schema 的公共 `CREATE` 权限已回收，应用账号不能修改 Schema。

macOS LaunchAgent：

```text
com.xrayai.axiom
com.xrayai.axiom.runner
com.xrayai.axiom.postgresql-backup
com.xrayai.axiom.postgresql-observer   # 生产切换后 24 小时验收期
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
 ├─ phase_capabilities 四阶段逻辑 Worker、能力矩阵和 Lease 快照
 ├─ execution      claim 后的 start/input/heartbeat/complete/release
 ├─ dispatch       Snapshot、恢复、领取
 ├─ artifacts      文件产物元数据与内容
 ├─ archives       Fact/Intent 下游链路的批次归档与恢复
 ├─ vulnerabilities 漏洞对象、状态、导出
 ├─ vulnerability_reviews 独立 ReviewRun、审核裁决与重审
 ├─ credentials    项目凭证
 ├─ dashboard      Dashboard 聚合只读模型
 ├─ workers        Worker 能力、并发和 Pi 配置
 ├─ settings       Runtime / Reason 触发配置
 ├─ mcp            Server 导入、发现、工具元数据和启停
 ├─ skills         共享 Skill 目录管理
 ├─ guard          监管决策和审计
 ├─ goals          Goal Evaluation
 ├─ audit          追加式审计
 ├─ diagnostics    任务健康、Worker、失败和 Token 统计
 ├─ token_usage    Pi Session 用量采集、幂等持久化、回填和聚合
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
  → stopped         人工停止或本次运行窗口到期；Runner 取消本地 Pi/RPC 进程
stopped
  → running         人工继续；选择新的单次运行窗口并按当前阶段重新进入调度

stopped/completed/failed
  → archived        保留全部历史，隐藏正常产物并停止后续 Worker 调度
archived
  → 原状态          人工恢复可见；不会自动 Resume 或创建 ReasonRun
```

Task 完成的唯一业务指标是 Goal Evaluation。没有 Intent、没有 Worker 或某一轮 Reason 结束都不能单独表示任务完成。

新建和 Resume 可提供整分钟 `run_duration_seconds`；省略或 `null` 表示不限时。每次 Resume 都覆盖旧窗口，按自然时间计算 `run_deadline_at`，排队、Reason 间隔和重试都计入。到期由 Runner 下一 tick 调用 Deadline Reconciler 原子暂停，默认精度约 3 秒；服务中断期间过期的 Task 会在恢复后的首个 tick 暂停。

Task 级归档由 `tasks.archived` 表示，和 Fact/Intent 的归档批次相互独立。运行中的 Task 必须先暂停；
仍有 `claimed/running/concluding` Intent、ReasonRun 或 ReviewRun 时拒绝归档。归档和恢复都不改写 Task
原状态、Goal Cycle 或子实体状态，恢复后也不自动继续主执行链。物理删除若受到执行历史外键保护，会返回
明确冲突并提示使用“归档任务”，不再暴露底层数据库错误。

## 4. 调度和领取

一次 Runner tick：

```text
读取 Runtime Settings
→ POST Task Deadline Reconciler
→ POST Reason Trigger Reconciler
→ recover-stale
→ 清理已完成 Future
→ GET dispatch/snapshot
→ Reconciler 从统一可领取工作视图计算 claim actions
→ WorkerAllocator 按 capability、Task worker_pool、disabled、cooldown、max_running 选择
→ API compare-and-swap claim
→ ThreadPoolExecutor 异步执行
```

统一工作视图同时包含 Bootstrap/Explore Intent、ReasonRun 和 Vulnerability ReviewRun。Task 内领取优先级为 `Bootstrap → Reason → Explore → Reviewer`；有可领取主链工作的 Task 也整体优先于仅有 Reviewer 后处理的 Task，Reviewer 使用剩余容量，不能抢占最后一个槽位而延迟主执行链。相同优先层内按最近一次领取时间轮询，每轮优先给每个 Task 一个工作，避免低 ID Task 长期占满槽位。ReasonRun 和 ReviewRun 都占用全局、模型后端与单 Task Run 槽位，但不计入 Intent 数量或 Explore 并发。

有效领取会同时校验 `global_max_active_runs`、`global_max_active_tasks`、`per_task_max_active_runs`、`per_task_explore_concurrency`、Worker `max_running`、能力和 Task Worker Pool。纯调度计算先过滤一次，数据库 claim 事务再校验一次，防止多个 tick/Runner 并发越限。Bootstrap、Reason 和 Reviewer 的单 Task 并发固定为 1。

Bootstrap、Reason 和 Explore 的 Claim 事务以及 Reason Trigger 会校验 Task 的绝对 UTC 截止时间，防止 Deadline Reconciler 与领取之间的竞争窗口产生新主链工作。Reviewer 是漏洞落库后的独立后处理：只要 Task 非历史只读、漏洞未归档，就可在 Task `running/stopped/completed/failed` 时继续领取，不受运行截止时间影响；Task 暂停也不会取消本地 Reviewer 进程。

所有 Dispatcher Snapshot、stale recovery、Reason Trigger 和阶段 start 事务同时排除 `tasks.archived=true`。
这层数据库/服务端校验不是单纯前端隐藏，可防止归档与领取之间的竞争窗口继续启动 Worker。

Reason 提交后只创建 `pending` Explore Intent，不指定具体 Worker。空闲且兼容的 Worker 会自动领取；容量不足保持 pending，不记失败，也不要求 Reason 重新规划。

### 四阶段逻辑 Worker 与执行后端

AXIOM 的 Worker 语义固定为四个逻辑阶段，而不是“一个模型后端就是一种 Worker”：

```text
Bootstrap Worker  ─┐
Reason Worker     ──┼─ 从各自阶段后端池选择具体模型执行后端
Reviewer Worker   ──┤
Explore Worker    ──┘
```

数据库中的 `workers` 行表示**模型执行后端**。同一后端可以加入多个阶段池；它的
`capabilities_json` 只表示参与阶段，模型、Base URL、Key、上下文窗口、思考模式、并发和优先级
仍属于该后端。调度选择为：

```text
当前阶段后端池 ∩ Task worker_pool ∩ 已启用 ∩ 非冷却 ∩ 有并发容量
```

Worker 概览区分阶段运行量和后端共享占用：阶段卡片的 `running` 只统计当前阶段，后端条目的
`shared_running` 统计该模型后端跨四个阶段的总活动 Lease，`available` 按共享 `max_running` 计算。

可分配能力的唯一运行时来源是 `phase_capability_bindings`，五个 scope 为
`common/bootstrap/reason/explore/review`。Skill、MCP Tool、扩展 Tool（API/数据库类型仍为
`axiom_tool`）的有效集合为：

```text
common 能力 ∪ 当前阶段专属能力
```

通用能力不能在单个阶段排除；若只想给部分阶段使用，应取消通用并显式绑定所需阶段。
`whatisvul` 是锁定且强制启用的通用 Skill。首次升级会把当时启用的 Skill 迁移为通用能力，
之后新建 Skill 和新发现扩展 Tool 均默认零绑定。旧 Worker 级扩展 Tool/MCP 写接口保留兼容路由，
但拒绝继续写入，不再形成第二套运行时真源。

内置 Tool 不写入绑定表，由代码注册表作为 API、前端和运行时共同来源。目录包括 Pi 原生
`read/write/edit/bash`、通用 Skill/Credential CLI、Reason Workspace CLI、Explore 漏洞上报 CLI，
以及 Reviewer 的 `axiom review status/decision submit`；
阶段及 Execute/Conclude 可用范围固定且不可修改。Bootstrap Conclude 没有 `bash`，其阶段 Prompt
和追加 System Prompt 都只允许复用 Execute 已读取的 `whatisvul/SKILL.md`。

每个新 Lease 在运行目录写入 schema 2 的 `phase-capabilities.json`，固化内置 Tool 目录、本阶段
有效 Skill、具体 MCP Tool、MCP Server 配置，以及扩展 Tool 的版本、包哈希和 Schema；存在扩展
Tool 时另写 `axiom-tools.json`。schema 1 历史快照继续可读，旧 Lease 不热修改，下一次领取才读取
最新矩阵。

MCP 以具体 Tool 为绑定单位：选择一个 Tool 时，Lease 只注册该 Tool 的稳定运行名，所属 Server
随调用按需启动，同 Server 的其它 Tool 不会披露给模型。扩展 Tool 通过 Pi Extension 加载，
Bootstrap/Reason/Reviewer 的 JSON 模式和 Explore 的 RPC 模式共用同一能力快照。Explore Conclude
继承本 Lease 的 Skill/MCP，但不加载扩展 Tool，也不构成第五种 Worker；Reviewer Conclude 继续使用
Reviewer 的同一 Pi Session，并同样不加载扩展 Tool。Pi 的 `--tools` 只包含固定 Pi 原生 Tool、本 Lease
选中的 MCP Tool 和扩展 Tool；AXIOM 内部 CLI 仍通过 `bash` 和活动 Lease API 校验提供。

Worker 页面右上角分为“能力管理 / 后端配置”。能力管理固定展示四个逻辑 Worker 卡片，点击卡片后
在同一个右侧抽屉集中查看概览、内置 Tool、Skill、MCP Tool 和扩展 Tool；后端配置只管理阶段后端池
和模型 API 配置。前端不暴露 `common` 的操作负担，而是直接显示 Bootstrap/Reason/Explore/Reviewer
四列：四阶段全选自动序列化为 `common`，取消其中一个阶段会自动转换为剩余阶段的显式绑定。所有修改先进入
草稿，确认后仅用一次矩阵请求原子保存。

MCP Tool 在抽屉中按 Server 折叠并支持 Server 级三态批量操作，但底层仍保存具体 Tool ID，不扩大
Server 授权。Skill、MCP、工具独立页面只管理资源内容、连接、启停、发现、版本和状态，只读展示阶段
徽章，并通过 `#/workers?mode=manage&phase=<phase>&tab=<tab>&capability=<id>` 深链返回唯一的能力编辑
入口。能力深链在 Worker 数据加载后消费，冷启动也会打开并定位对应能力；普通 `#/workers` 清除旧
抽屉状态。

Intent、ReasonRun 和 ReviewRun 都使用独立 `lease_id`。领取后先启动心跳，再初始化 Context、运行目录和 Pi；执行中持续 heartbeat。执行重试和基础设施重试分开计数。Intent/ReasonRun 的终态可成为下一轮 Reason 的输入信号；ReviewRun 的终态只更新漏洞审核历史和状态，不自动触发 Reason。

Runner 对 API 暂时不可用捕获 `ControlPlaneError`，保留进程并按下一轮间隔重试。API 重启演练已验证 Runner PID 不变、API 恢复后继续调度。

## 5. 四个阶段

当前流程保留 Bootstrap，不使用 ASM 替代：

```text
Bootstrap → ReasonRun → Explore → ReasonRun → Explore → ... → Goal
                         └─ 待验证 Vulnerability → ReviewRun → 审核状态
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

Pi JSON 模式不能只依据外层退出码判断成功：Provider 自动重试耗尽时，Pi 可能输出 assistant
`stopReason=error` 或 `auto_retry_end success=false`，但仍以 0 退出。Adapter 会解析最后一个 assistant/
自动重试终态；只有最终仍失败时才抛出 `TransientWorkerError`，早期错误后成功会清除错误状态。该检查发生在
assistant 文本提取和 FINISH/Workspace 校验之前，防止 429/5xx 被误记为 `reason_plan_invalid`。

Reason 的 Provider transient 错误直接废弃 Workspace 并释放 Lease，不进入三次 Conclude；失败分类为
`worker_infrastructure/transient`，只增加 `infrastructure_retry_count`。`ReasonService.release_run()` 同时按
现有 `worker_failure_cooldown_seconds` 冷却故障 Worker，调度器优先选择其它健康 Reason 后端。

每个命令只提交一个小操作并立即校验、持久化。Reason 执行期间 Todo 更新和 Intent proposal 只存在于 Workspace，正式 Intent 表和图谱不变化。每个 Intent 的 `execute_seconds` / `conclude_seconds` 是结构化字段，不写入 description；Reason 不选择 Worker，也没有漏洞上报入口。

Reason Prompt 对规划粒度有硬性文字约束：一个 Todo 只承载一个攻击主题，一个 Todo/Intent 最多包含 2–3 个紧密相关的独立测试分支；获取对象 ID、准备登录态和调用前置接口等同一攻击链步骤不单独算分支。超过三个方向必须拆开，额度不足的方向保留为 Todo 交给后续 ReasonRun。时间预算仍由 Reason 按每个 Intent 的实际复杂度单独决定，Prompt 不再提供可被机械复制的固定 `600/120` 示例。

每轮新增 Intent 由后端强制限额：

```text
remaining = min(
  reason_max_new_intents_per_run,
  reason_target_open_intents - 当前开放 Explore 数
) - 当前 Workspace proposal 数
```

开放 Explore 包括 `pending/claimed/running/concluding`。额度耗尽时 CLI 立即返回当前开放数、目标数和剩余额度；commit 在同一事务中再次校验。Todo 数量不与 Intent 额度绑定。

Reason 最后一条 assistant 文本精确等于 `FINISH` 后，Runner 先读取 Workspace readiness：必须存在 Goal assessment；Goal 达成时必须有真实 Fact 证据且不能继续派发；Goal 未达成且 0 proposal 时，至少要把没有后续工作的可执行 Todo 明确收口，材料性与阻塞判断由 Prompt 合同约束。校验通过才调用 `/commit`。提交后由代码生成 `reason_plan.json` 审计文件；`/submit` 不再存在。

ReasonRun 创建时还会快照 `todo_review_ids`：这些 Todo 处于 pending/active、至少有一个终态 Explore，且已经没有开放 Explore。Reason 必须逐个明确收口为 completed/rejected/obsolete；若保持 active，必须填写原因并在同一 Workspace 为该 Todo 提交新的 Intent。readiness 会拒绝遗漏审查、空 active，以及“终态 Todo 同时派发 Intent”。审查集合固定在 ReasonRun 快照中，不会因并发 Explore 在执行期间结束而动态扩大。

`axiom reason status` 同时返回本轮 `source_fact_ids`。列表非空时，Reason 必须逐个读取新 Fact，并在 Workspace 中通过 Todo source、Intent `from_facts` 或 Goal evidence 消费，防止新事实被静默遗漏；列表为空时仍可依据当前 Goal、人工续跑原因/重点和历史 `findings.md` / `leads.md` 规划，不能把“没有新 Fact”当作没有工作的证明。处理完新 Fact（若有）和 `todo_review_ids` 后，才允许新增 Todo 或 proposal；语义关联仍由 Reason 判断，后端不做模糊匹配。

Todo 是长期攻击面索引。Reason 新增 Todo 前必须对照所有状态的历史 Todo；已有同义主题时复用原 Todo。`completed/rejected/obsolete` Todo 在新 Fact、当前 Goal 变化、当前 Goal Cycle 的人工说明/重点，或 findings/leads 明确留下尚未执行且当前可执行的材料性方向时，可以恢复为 `active`，但必须复用原 Todo ID、填写新的状态原因，并使用真实已有 Fact 作为来源。派发 Intent 前通过 `graph.json` 的 `todo_id` 检查该 Todo 的开放和历史 Intent；只有增加新对象/角色、新端点/目标、新攻击条件、新 Fact 支撑方向，或 findings/leads 中明确未执行的具体验证分支时才允许继续派发。该规则属于 Prompt 驱动的语义决策，不等于后端自动语义去重。

同一轮新增 Todo 可以使用临时 `ref` 被 Intent 或 Goal Assessment 引用。提交事务先创建 Todo，再将临时引用解析为真实 `tdXXX`；引用不存在时整批回滚，不会留下半套规划或错误的 Goal Evaluation。

Reason Trigger Reconciler 在 Bootstrap/Explore 完成后立即评估，并在每个 Runner tick 周期评估。前置条件是 Task running、Bootstrap 已完成、Goal 未达成、没有活动 ReasonRun，且除初次外已满足最短间隔。满足以下任一条件时创建新 ReasonRun：未规划 Fact 达到批量数；开放 Explore 低于低水位且存在新 Fact、失败终态或未派发的可执行 Todo；最老未规划 Fact 达到最大等待。输入状态指纹防止同一低水位状态空转，Task 恢复只重新评估规则，不无条件触发。

输入指纹去重同时覆盖 `completed` 和重试耗尽后的 `failed` ReasonRun，避免相同终态输入在每个 tick 被无限重建；人工停止产生的 `cancelled` 不参与去重，因此恢复 Task 后仍可按当前事实重新评估。

每轮都必须提交 Goal assessment。Goal 达成时禁止 proposal，并要求至少一个真实 `fact:<id>` 证据；Goal 未达成时允许 0 个新 Intent，但只有两种合法情况：确实没有材料性方向，或方向只受预算、工具、凭证、外部条件阻塞且没有可执行替代。只要存在材料性、当前可执行且没有同义开放 Intent 的方向，就必须复用/恢复原 Todo 并在额度允许时派发非重复 Intent。完成事务创建 Goal Evaluation；达成时取消其它开放 Intent/ReasonRun 并结束 Task。新图谱不创建 Reason 节点，当前 Goal 证据直接显示为 `Fact(s) → Goal`；历史 Reason Intent 保持只读原样。

已完成 Task 续跑时会创建新的 `task_goal_cycles` 轮次，将新 Goal 设为唯一当前目标，不重新 Bootstrap，也不自动恢复上一轮 cancelled Intent。Continuation ReasonRun 复用历史原始 Fact、Todo、Intent、Artifact 和漏洞，但 Reason Context 刻意不披露历史 Goal Cycle、Goal Evaluation 及其证据，避免旧的达成结论锚定新判断。人工续跑原因和重点从当前 Goal Cycle 注入本轮以及后续 ReasonRun，属于有效规划依据，但不是 Fact，不能伪造为 Todo source、Intent `from_facts` 或 Goal evidence。历史 Goal 和评估仍保留给人类在概览、图谱和详情中查看。

Task stop、人工/Reason Goal 完成、ReasonRun release 和 stale recovery 都会在同一事务中把对应 `open` Workspace 标记为 `abandoned`，旧 lease 随即失效。当前正在完成 Goal 的 Workspace 会被排除在批量废弃之外，并在事务尾部正常标记为 `committed`，避免出现终态 Task 下仍可写入的僵尸 Workspace。

### Explore

Explore 执行一个明确 Intent，可以为完成当前 Intent 寻找必要依赖，但不能越过 Task Scope。产出：

```text
{run_dir}/artifacts/
├── findings.md
├── leads.md
└── evidence/**
```

`findings.md` 保存本轮已确认事实和确认的漏洞发现，`leads.md` 保存已经发现但尚未验证或尚未完成的方向；二者必须始终存在且非空。没有对应内容时明确写“本轮无新增确认事实”或“本轮无遗留待验证线索”。请求、响应、工具原始输出、截图和漏洞证明只写入可选的 `evidence/**`。

Prompt 仍要求 Worker 最后一条 assistant 文本只包含 `FINISH`；Execute 正常返回时，Runner 继续使用 Explore 专用的有限格式归一化。进入 Conclude 后则以保住已有结果为优先：若收口最终缺少 FINISH、格式错误或超时，只要两个核心文件仍是安全、非空普通文件，本轮就直接提交并记录 `explore_closeout_core_fallback` warning，不再释放后从头重试。只有 `findings.md` 或 `leads.md` 缺失、为空或不安全时才拒绝完成。

Runner 的正式扫描范围仍只有两个核心文件和 `evidence/**`。Evidence 是可选审计材料：空文件、危险名称、symlink、越界/隐藏路径会被跳过且不读取；`analysis/`、`reproduction/` 和其它根目录内容不注册；合法 Evidence 按稳定路径最多注册 48 个，使单轮总 Artifact 保持最多 50 个。跳过项写结构化 warning，但不会阻止核心结果完成。正式存储直接形成 `{intent_id}/findings.md`、`{intent_id}/leads.md` 和实际采纳的 `{intent_id}/evidence/**`，Artifact 类型分别为 `explore.findings`、`explore.leads`、`explore.evidence`；历史 `explore.analysis`、`explore.reproduction` 只读保留。

只有达到漏洞标准时，Explore 才使用 `axiom explore vulnerability report` 逐条上报 Vulnerability。`--evidence-path evidence/...` 现在可省略或重复；模型不能设置漏洞状态，服务端自动补齐 `intent_id`、`source=explore`、`reported_by` 和 `status=待验证`。路径先进入 lease 级 Explore 状态，收口时只把实际成功注册的 Evidence 解析为正式关系；缺失、为空或被跳过的引用只记录 warning。即使没有有效 Evidence，漏洞仍关联本轮 `findings.md` 并以“待验证”落库。Reason 不上报漏洞。

漏洞表保存标题、类型、等级、目标、描述、前置条件、影响、修复建议、状态和来源元数据；详细发现只写入 findings，原始材料只保存在 evidence。Explore Worker 上报时 `prerequisites` 与 `remediation` 是非空必填字段，CLI 和执行 API 都会在落库前拒绝缺失或空白值；人工新建/编辑仍允许留空，历史空值不回填。`vulnerability_artifacts` 使用 `finding/evidence` 角色关联正式 Artifact，新漏洞不再写 `reproduction_steps`、`curl_command`、自由文本 `evidence` 或 `source_file`。旧漏洞保留 V1 列并通过只读 `legacy` 区域展示，不批量复制文件。

Explore Fact 由代码使用原 Intent description、findings/leads Artifact ID、证据数量和本轮 Vulnerability ID 生成，不使用模型尾部文本。`explore_result.json` 只是代码维护的内部状态/索引，不要求 Worker 编写，也不注册为 Artifact。

Explore 是当前唯一使用 Pi RPC 模式的阶段。Runner 通过 stdin/stdout JSONL 发送初始 `prompt` 并持续读取 Pi 活动事件；当本 Intent 的 Execute 预算消耗到动态 70% 时发送一次 `steer`，例如 600/1200/1800 秒分别在 420/840/1260 秒提醒。提醒要求停止扩展新方向、立即整理 findings/leads、把未完成分支如实写入 leads，并明确禁止为了补 Evidence 重跑大型测试；它只进入模型上下文，不延长硬超时、不改变 lease，也不改变工具权限。若阶段已提前结束则不发送；发送或 Activity 上报失败只记录警告。Task 停止、lease 失效或硬超时时，RPC Supervisor 发送 `abort` 并清理整个进程组。

Pi RPC 的会话完成边界是 `agent_settled`，不是 `agent_end`。一次会话可以依次出现多个 `agent_end`，其后仍可能发生 Pi 内部自动重试、Compaction 或处理排队的 steer；Supervisor 因此只把 `agent_end` 当作单轮活动事件。收到 `agent_settled` 后，Supervisor 才发送 `{"id":"axiom-final-text","type":"get_last_assistant_text"}`，并把成功响应中的 `data.text` 作为唯一权威最终文本。settled 前 EOF、最终文本响应前 EOF、查询失败或缺失文本均属于 RPC 协议错误；Pi 的真实非零退出码不会被完成事件覆盖成 0。

`process/stdout.log` 在 RPC 模式下是过滤后的协议诊断日志，不再是完整事件镜像。Pi 的 `message_update`、`tool_execution_update` 和 `bash_execution_update` 都会携带累计内容，逐行原样保存会使日志随生成长度近似 O(n²) 增长；Reader 因此只保留响应、生命周期、重试、Compaction、工具开始/结束等诊断事件，并在 EOF 追加 `axiom_rpc_log_summary` 记录各类省略数量。控制队列也只接收 Supervisor 真正需要的 `response`、`agent_settled` 和 EOF。完整消息、Tool Call、Tool Result 与流式历史仍以 Pi Session JSONL 为唯一权威审计源，实时活动也继续增量读取 Session，不依赖被过滤的进程日志。

生产 Pi 运行时固定在忽略版本控制的 `runtime/pi/0.82.1/`，启动器 `runtime/pi/0.82.1/bin/pi` 明确使用 `/Users/hexin/.workbuddy/binaries/node/versions/22.22.2/bin/node`，不依赖 `/usr/local/bin/pi` 的 shebang 或 LaunchAgent PATH。所有 Pi Worker 的既有 `config.binary` 均指向该启动器；Bootstrap、Reason 只升级运行时，仍使用 JSON 模式。

Runner 创建阶段 Executor 前仍强制校验固定 Pi/Node 路径和版本，但生产 Pi 版本从安装目录的 `@earendil-works/pi-coding-agent/package.json` 本地元数据读取，不再执行可能触发完整 CLI 初始化的 `pi --version`。这避免冷启动超过 5 秒时把健康 Bootstrap Lease 错误释放为 `runner_dispatch_failed`。校验同时确认包名、版本、`bin.pi` 和包内可执行 CLI 入口；不提供 `--offline` 或其它启动式回退。Node 的固定绝对路径继续使用轻量 `--version` 校验。成功结果在 Runner 进程内缓存，不影响阶段预算、Lease 或 Pi JSON/RPC 协议。

Pi Worker 的模型运行参数只保留真实参与运行的 `context_window`、`thinking_enabled` 和 `thinking_level`。`context_window` 写入 lease 隔离目录的 `models.json.contextWindow`，用于 Pi 的 Token 预算与自动 Compaction，不能扩大上游模型的真实上下文上限。思考开关关闭时，模型元数据写入 `reasoning=false` 且命令行显式传递 `--thinking off`；开启时写入 `reasoning=true` 并传递 `minimal/low/medium/high/xhigh/max` 中的所选等级。Pi 0.82.1 对 `xhigh/max` 要求模型显式 opt-in，因此 AXIOM 会为用户选中的高等级生成对应 `thinkingLevelMap`；上游兼容服务仍可能忽略或拒绝自身不支持的等级。历史单字段 `thinking=off|level` 在读取和下一 Lease 时自动拆分，用户保存 Worker 后持久化为两个独立字段；旧的伪能力测试字段不再披露或保存。配置不会热改已经运行的 Pi 进程，只对新 Lease 生效。

Worker 的安全工具可用性由受控 PATH、工具附属数据环境和 Skill Probe 三层共同保证。`build_tool_path()` 仍避免 Axiom venv 中的同名 console script 覆盖 ProjectDiscovery 等安全工具；`discover_tool_environment()` 在每个 Pi 进程和 `axiom skill doctor` 中复用，只在完整验证 `nmap-services`、`nmap-protocols`、`nse_main.lua` 与 `scripts/script.db` 后自动注入 `NMAPDIR`，且不会覆盖人工设置。这样 `nmap --version` 成功但 NSE/服务数据库实际不可用的状态不会再被当成健康。

Skill frontmatter 的 `requirements.probes` 是无 shell 的命令数组，支持 `{skill}` 路径替换、超时、返回码和输出包含检查。Nmap 使用 `nmap --script-help default` 验证数据与 NSE；Nuclei 使用禁用更新检查的模板校验；Katana 验证当前安装真实支持的参数；Browser-control 直接检查共享 CDP。Skill 文档中的 Nuclei/Katana 示例也统一使用当前 CLI 参数和 `-duc`，避免自动更新、废弃参数或错误输出格式占用 Explore 预算。2026-08-03 对生产启用的 14 个 Skill 执行 Doctor，结果为 14/14 通过。

共享 Chrome-SecTest 仍是跨 Lease 持久化的浏览器，不归任何单个 Worker 进程组管理。`launch-chrome.sh` 通过 Python `subprocess.Popen` 启动 Chrome，并显式使用 `DEVNULL` 关闭标准输入、输出和错误，设置 `start_new_session=True` 与 `close_fds=True`；因此即使命令被 `head`/`tail` 等管道包裹，Tool Call 也会在 CDP 就绪后正常收到 EOF，Chrome 则继续独立存活。CDP 就绪后脚本重新按专用 Profile 与端口扫描实际 Chrome PID 再写 PID 文件；启动失败只清理同时匹配这两个条件的进程，不操作个人 Chrome。`CHROME_BIN` 仅作为测试或非标准安装路径覆盖。

Chrome PID 扫描会排除带 `--type=` 的 Renderer/Helper，只登记专用 Profile 和 CDP 端口匹配的主进程；停止时按该主进程的 PGID 清理整个浏览器进程组。Explore 启用 `browser-control` Skill 后不再让模型通过 Bash 调用 `cdp_ctl.py`，而是由 Pi 的 `axiom-browser` Extension 注册 19 个严格 Schema 的 `browser_*` 原生 Tool。Runner 内的异步 Browser Broker 通过权限为 `0600` 的 Unix Socket 接收调用，并在同一 Runner 生命周期内只建立一次 Playwright CDP 连接；模型只看到 Lease 私有的 `page_id` 和 Snapshot `ref`，看不到 CDP、Lease Token 或 Session ID。

Browser Session 与 Explore Lease 一一对应，页面从 Execute 保留到 Conclude，并在整个 Intent Lease 完成、失败、丢失或任务停止后统一关闭。同一页面操作串行，不同 Lease 页面可并行；`browser_list_pages`、页面操作和关闭都只能访问当前 Lease 页面，不存在默认页面、URL 子串或其它 Lease 页面回退。Popup、下载、HTTP、WebSocket、Console、Page Error 和 Dialog 自动归属触发页面。导航会增加 `document_generation` 并使旧 Ref 全部失效；Broker 内部断线重连后也会清空 Ref，要求模型重新 Snapshot。

`browser_snapshot` 提供 `interactive/viewport/subtree/full/changes`，每条 Ref 只披露 `role/accessible_name/text/attributes/allowed_actions`，并支持 iframe 和开放 Shadow Root；关闭 Shadow Root 明确列为能力限制。浏览器动作必须使用准确 Ref，服务端会验证 Ref 的代数、存活状态和 `allowed_actions`。`browser_read_page` 只返回受限的渲染文本；截图可添加 Ref 标签，但首版没有视觉问答或视觉找元素。

Browser Broker 把当前页面的流量和诊断记录写入本 Lease 的 `process/browser/`，`browser_read_network` 与 `browser_read_console` 使用增量游标，不再清空共享文件；敏感 Header 默认在 Tool Result 中脱敏。Browser-control 的 MITM 代理仍由 `launch-proxy.sh/stop-proxy.sh` 独立管理，只作为人工诊断和历史脚本兼容能力，不再是 Worker 浏览器流量的默认来源。`cdp_ctl.py` 继续保留给运维和旧脚本，但 Lease 环境中缺少明确 `--tab-id` 会直接失败，别名只解析当前 Lease 注册页面，`select` 使用与快照一致的全局元素索引。

Execute 超时后，Explore Conclude 使用同一 Pi session ID 启动新的 RPC 进程继续有界收口，但不会再次发送 70% 提醒。Explore 收口预算的正式下限为 180 秒、上限默认为 600 秒；Reason 提交低于下限的 Intent 会被 API 明确拒绝，不做静默修正。

### Reviewer

漏洞以 `status=待验证` 和 Finding/Evidence 正式落库后，系统在同一事务创建独立的
`vulnerability_review_runs`。Explore 漏洞只在 Explore Artifact 已完成注册时创建 ReviewRun，人工新建
的待验证漏洞则在漏洞创建事务内立即排队，因此 Reviewer 不会读取半成品。历史未归档、非只读 Task 的
待验证漏洞由迁移 `20260804_0034` 补建 `trigger=migration` 的 pending ReviewRun。

ReviewRun 状态机为：

```text
pending → claimed → running → concluding → completed
                    └──────────────→ failed / cancelled
```

同一漏洞最多一个活动 ReviewRun。关键漏洞字段、Finding 或 Evidence 关系变化时，旧活动 Lease 会被取消，
漏洞恢复为待验证，并按新输入指纹创建 `trigger=updated` 的 ReviewRun；人工直接设置 `已验证/重复/忽略`
属于人工裁决，会取消活动审核。`needs-human` 的结果继续保持待验证，但不会自动循环排队；人类可从漏洞详情
点击“重新审核”创建 `trigger=manual` 的新 ReviewRun。

Reviewer Context 在普通 Manifest 索引之外增加唯一的 `review_target.json`，包含目标漏洞完整字段、Finding
和 Evidence 元数据，以及每个正式 Artifact 的本地 `content_path`。Reviewer 还会读取 Task Scope、Hint、
Knowledge、全部未归档漏洞紧凑列表、来源 Intent/Fact/Artifact，以及本阶段有效的 Skill、MCP Tool 和扩展
Tool。`whatisvul` 是锁定的全阶段 Skill；`vuln-review` 默认仅绑定 Reviewer，提供主动复现、关键对照、证据核查和重复判断方法。

Reviewer 将 Finding/Evidence 视为待验证线索，而不是默认可信结论。目标可达、Scope 允许且操作安全时，
每个 ReviewRun 默认至少执行一次新的独立验证和一个单变量对照；`verified` 原则上需要本轮新鲜复现，
`ignored` 也必须有复现反证、对照结果或完整直接静态证据。缺少身份、环境或安全复测条件时应选择
`needs-human`，不得只因旧材料不足而直接忽略。Conclude 会阻止没有独立验证或明确例外理由的
文件式 `verified/ignored` 裁决。

`vuln-review` 使用 CVSS v4.0 Base Metrics 风格的维度核查利用条件和实际影响：
`AV/AC/AT/PR/UI`、受影响系统 `VC/VI/VA`、后续系统 `SC/SI/SA`。这些维度用于防止遗漏前置条件、
身份边界和 C/I/A 影响，不自动生成分数，也不允许 Reviewer 改写漏洞原严重等级。只有所有 Base Metrics
都有证据时才可附参考 Vector；未知项必须明确记录。Skill 的 `references/` 下分别提供
`verified/duplicate/ignored/needs-human` 四份完整 `review.md` 示例。模型形成候选裁决后必须读取对应附件；
在两个状态之间犹豫时读取两份，不能只读取 Skill 正文或复制示例结论。

Reviewer 的固定 AXIOM 操作为：

```text
axiom review status
axiom review decision submit verified --reason "..."
axiom review decision submit duplicate --duplicate-of vXXX --reason "..."
axiom review decision submit ignored --reason "..."
axiom review decision submit needs-human --reason "..."
```

所有裁决必须有可审计原因。`duplicate` 只能引用同 Task、未归档且创建时间早于当前漏洞的记录。Reviewer
只能写入 ReviewRun 的决定并在完成事务中更新漏洞状态、`status_reason` 和重复引用，不能改写漏洞标题、
类型、等级、描述、前提、影响、修复建议或原始 Evidence，也不能创建 Fact、Todo、Intent 或触发 Reason。

Reviewer 必须生成 `{run_dir}/artifacts/review.md`，可选生成 `artifacts/evidence/**`。完成时两类文件分别
注册为 `review.report` 和 `review.evidence`，存储在 `tasks/{task_id}/reviews/{review_run_id}/`，并通过
`artifacts.review_run_id` 归入文件中心的 Reviewer 文件夹；新增 Review Evidence 会作为正式 Evidence
关联目标漏洞。最终文本严格匹配 `FINISH`，缺少结构化裁决或 `review.md` 时在同一 JSON Pi Session 内
有界收口，不能使用 Explore 的核心文件兜底。

Reviewer 共享全局运行数、活动 Task 数、模型后端容量和 Task Worker Pool，每个 Task 同时最多一个审核。
它不属于 Task Goal 主链：Task 完成、失败或暂停后仍可继续后处理，Task 暂停不会发送 Reviewer cancellation；
漏洞归档则取消活动 ReviewRun，恢复一个仍为待验证的漏洞会重新排队。Reviewer 完成不会主动创建 ReasonRun，
后续自然发生的 Reason 只会在新 Context 中看到更新后的漏洞状态和原因。

漏洞详情展示 ReviewRun 历史、Worker、审核时间、原因、重复漏洞蓝链和 Review Artifact 深链。外部预览仍可
用于检查 payload，但正式外部上报和失败重试只接受 `status=已验证` 的漏洞；重复、忽略和待验证记录均由
服务端拒绝外发。

### Token 用量持久化

Pi Session JSONL 是实时和历史 Token 用量的主要事实源。Session tailer 在 Bootstrap、Reason、Explore、Reviewer 的 JSON/RPC 执行中共用，解析 assistant message、toolResult、compaction 和 branch summary 中明确报告的 usage；不从文本长度估算，也不递归读取 `retainedTail`。明细写入 `llm_token_usage`，以 `session_file + session_id + session_entry_id + usage_kind` 的稳定键幂等去重；HTTP 上报失败只写入当前 Lease 的 `process/token-usage.pending.jsonl`，不会让阶段失败。

每条用量保留任务、Intent/ReasonRun/ReviewRun、Lease、阶段、Worker、provider、实际响应模型、Session entry 和四类 Token：未命中输入、缓存命中、缓存写入、输出。总量为四类之和，`reasoning` 是输出子集，不重复加入；缓存命中率为 `cacheRead / (input + cacheRead + cacheWrite)`，分母为零时为空。`llm_token_usage_sources` 记录 Session 是否完整；路径缺失、JSONL 截断、字段无法恢复的历史来源标记为 `incomplete`，不伪造缺失数量。

Dashboard 和任务 Diagnostics 通过已有汇总接口支持时间范围和实际 `provider/model` 筛选，任务明细覆盖所有 Goal Cycle、成功、失败、超时、重试、Execute 和 Conclude。`axiom token-usage-backfill` 扫描 `runs/**/sessions/*.jsonl` 并复用同一幂等写入服务；旧 Audit Event 的 Token 只作为标记为 `legacy/incomplete` 的兼容来源，已有 Session 记录时不重复导入。

2026-07-27 的生产受控验收确认了两种路径：60 秒 Execute 在约第 42 秒产生 `soft_reminder`；提醒若落在工具调用中，会由 Pi 排队并在工具结束后的下一轮模型调用前注入。验收会话中该 steer 作为 user 消息落入 session，模型随后仅返回 `FINISH`，Intent 在 49.7 秒内完成且没有进入 Conclude 或重试。另一轮故意让慢请求撞上 Execute 硬超时，验证了同 session RPC Conclude 可继续整理并原子提交 Artifact/Fact。

后续 `t_147` 生产受控验收专门覆盖 Pi 自身重试：本机兼容服务第一次返回 HTTP 500，Pi 自动发起第二次请求；动态 70% steer 被接受后触发第三次模型调用。最终 RPC 日志包含 2 次 `agent_end`、1 次 `auto_retry_start/end`、1 次 `agent_settled`，随后 `get_last_assistant_text` 返回精确 `FINISH`，代码成功注册 `explore.findings`、`explore.leads` 并生成 Fact。临时 Task、Project、Worker、服务和文件随后全部清理，正式 Runner 保持运行。

2026-07-28 在生产 `t_148` 上验证了日志与浏览器修复：共享 Chrome-SecTest 以 `PPID=1`、独立 PGID 和三个 `/dev/null` 标准 FD 运行，管道包裹的启动命令可立即返回；新 RPC Lease 不再记录三类累计流式事件。`i014` 完成后的诊断日志为 47,107 B、Session 为 432,577 B，EOF 摘要记录省略 1,135 条 `message_update` 和 7 条 `tool_execution_update`，仍正常经过 `agent_settled → get_last_assistant_text(FINISH)` 完成 Intent 并生成 Fact。

### 阶段完成协议

四个阶段的 Prompt 都要求最后一条 assistant 文本只包含：

```text
FINISH
```

Bootstrap、Reason 和 Reviewer 严格匹配：工具输出、代码块、`FINISH` 前后附加说明或大小写变化均不算完成。Explore Execute 使用有限归一化，可接受无害 Markdown 包装或前置说明后以独立 FINISH 结尾；非精确通过会记录 warning。

`FINISH` 通常只触发产物/Workspace/审核裁决校验，不直接改状态；缺失项会在同一 Pi 会话和共享 conclude deadline 内最多收口 3 次。Explore Conclude 是唯一例外：收口最终没有 FINISH、格式错误或超时时，只要 `findings.md` 与 `leads.md` 已有效落地，代码就以核心文件为完成依据继续原子提交。Evidence 永远不能替代两个核心文件，也不再成为 Explore 成功门槛。Bootstrap、Reason 和 Reviewer 没有该兜底；Reviewer 还必须同时具备结构化裁决和非空 `review.md`。

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
 ├─ goal_evaluations.json   # Bootstrap / Explore / Reviewer
 ├─ goal_cycles.json        # Bootstrap / Explore / Reviewer
 └─ review_target.json      # 仅 Reviewer
```

Fact、Intent、Artifact 和 Goal Evaluation 等渐进式索引仍保留 preview 与 detail ref。`todos.json` 已提供完整 `title/status_reason`，`hints.json` 和 `knowledge.json` 已提供完整正文，`vulnerabilities.json` 已提供完整标题及去重判断字段，因此这四类高价值列表不再重复生成 preview，但继续保留 `ref/detail` 以兼容按需展开。`graph.json` 的 Intent 摘要额外提供 `todo_id`，使 Reason 能先做历史主题对照，再只展开相似 Todo/Intent。Reason phase 的 Manifest 不包含 `goal_evaluations` 与 `goal_cycles` 索引，也不会生成这两个文件；当前 Goal 仍直接存在于 Manifest，续跑原因/重点由 ExecutionInput 注入。Reviewer 在完整通用索引之外额外获得唯一目标文件 `review_target.json`，其中 Finding/Evidence 带受控本地 `content_path`。Bootstrap/Explore/Reviewer 保持历史 Context 索引兼容。Worker 仍通过 detail 文件或 Context expand API 获取长内容，不会把整个图谱和产物正文重复塞进上下文。

新 Context Revision 会排除已归档 Fact、Intent、Todo、Artifact 和 Vulnerability；Intent 的来源/产出、Todo source、Artifact/Vulnerability 的 `intent_id` 也会移除指向归档实体的引用。引用归档证据的历史 Goal Evaluation 不进入 Worker Context，`/context/expand` 对归档实体返回不存在。已有 Lease 的不可变快照不回写，下一 Lease 才应用新的可见集合。

## 7. 文件和产物

FileStore 使用外部 data-dir，Artifact 表只保存元数据、hash、size 和 storage key；文件内容仍进入现有文件管理体系。任务删除、Artifact 归档和迁移均不允许通过路径穿越访问外部文件。

阶段文件采用 prepare → 文件提交 → 数据库事务 → finish 的收口顺序。数据库事务成功后删除 Worker 运行目录中的原文件，正式 Artifact 内容只保留一份；事务失败则回滚暂存文件并保留运行目录证据供同 lease 收口重试。Reviewer Artifact 使用 `review_run_id` 作为所有者，在文件中心形成独立的 `vrXXXXXX` 文件夹，不会混入“任务文件”或伪装成 Intent。内部索引、Pi session、进程日志和模型配置永远不会被自动注册为 Artifact。

项目凭证、Task Hints、Project Knowledge 分别保存；凭证按阶段执行输入按需提供，不混入 Origin，也不写入 Fact、Artifact 摘要或命令参数。

### 链路级联归档

Fact/Intent 归档使用 `archive_batches + archive_batch_members`，不是直接翻转单条 `archived`。Fact 根会包含其生产 Intent、自身和所有下游 Intent/Fact；Intent 根会包含自身、产出 Fact 和后续下游，但永远不反向归档来源 Fact 或旁支。闭包内 Intent 的 Artifact、Vulnerability 和漏洞证据 Artifact 一并归档；Todo 仅在没有其它可见关联 Intent 时归档。Bootstrap Intent、Bootstrap 产出 Fact、`origin` 和 `goal` 受保护，活动 Intent 会阻塞归档。

预览返回 `closure_hash`，提交在 SQLite `BEGIN IMMEDIATE` 中重算；闭包变化返回 409。恢复按批次执行，仍被其它 active 批次覆盖的成员继续保持归档。归档不删除文件、Session、Audit Event 或历史 Context；恢复也不改写 Intent/Todo/漏洞原业务状态。若归档与活动 ReasonRun 的 source 或 open Workspace 引用相交，ReasonRun 会被取消且 Workspace 被废弃；归档漏洞会取消其活动 ReviewRun，并把历史 Review Artifact 纳入归档闭包。恢复后只有仍为待验证的漏洞会创建新的 ReviewRun。无关 Explore 和 Reviewer 不受影响。

### Task 级归档

Task 级归档是整项任务的可见性与调度开关，不创建 `archive_batches`，也不递归翻转子实体的 `archived`。
归档期间 Task 基础壳信息和 Audit 历史保留，但 Hint、Graph、Todo、Goal、Artifact、Vulnerability 和 Context
读取统一返回冲突；跨 Task 的文件中心、漏洞列表和项目活动统计也排除该 Task。历史文件、数据库记录、
Session 和不可变 Context Revision 不删除，恢复 Task 后原内容原样重新可见。

新增接口为 `/tasks/{task_id}/archive` 和 `/tasks/{task_id}/unarchive`。Task 列表保留“已归档”徽章与恢复
入口；概览、图谱和原有归档批次视图在 Task 归档期间只显示锁定说明。详细边界见
`docs/2026-08-05-Task级归档与产物隔离.md`。

## 8. 前端

前端继续使用 Alpine 和原有图谱视觉，通过 V2 API 适配层对接模块化后端，不引入 React/Vue。Tailwind 固定为 `3.4.17` 并在开发时编译为 `tailwind.generated.css`；生产浏览器不再加载 Tailwind Runtime，也不依赖 Node。动态状态类通过显式 safelist 进入构建产物，连续构建必须产生相同文件。

桌面侧栏默认宽 176px，显示图标和文字；可折叠为 56px 纯图标模式并写入 `localStorage`。移动端使用汉堡按钮和抽屉导航，选择页面后自动关闭。主内容宽度由侧栏状态联动，不使用固定 margin。

页面使用单一 `#page-host` 按路由挂载。切换页面会 Abort 旧请求、执行 `Alpine.destroyTree()`、销毁 Cytoscape、Minimap、共享图谱动画和旧 DOM；模板文本只做内存缓存，未激活的 13 个页面不再常驻 DOM。任务详情 Human Tab 使用 `x-if`，只渲染当前 Workspace/Todo/Intent/Fact/Artifact 面板。

任务详情提供“概览 / 图谱 / 归档”三种视图；归档视图按批次展示根节点、原因、操作者、成员类型、关系、Artifact 预览/下载和恢复历史。Fact/Intent 归档先展示闭包、阻塞项和影响数量，再原子提交；停止或完成 Task 仍可整理历史链路。Task 列表和三个详情视图均提供 Task 级归档/恢复入口；归档 Task 只显示基础信息和恢复操作，其正常内容、文件和漏洞在恢复前不披露。

任务详情的“概览”和“图谱”是两种人类视图：图谱不是执行事实源；右侧 Todo、事实、产物和漏洞分别从 V2 API 读取。ReasonRun 只显示为图谱上方的运行活动卡片，可查看 Worker、状态、当前活动、工具、心跳、触发器、预算和配置快照，不创建图谱节点。

漏洞详情新增 Reviewer 审核区，按时间倒序展示 ReviewRun 的等待、启动、审核、收口、完成、失败和取消状态，
以及 Worker、触发来源、审核时间、结构化决定、审核原因、重复目标和 Review Artifact。重复目标复用漏洞精确
深链；`review.report/review.evidence` 复用文件中心深链并定位到对应 ReviewRun 文件夹。没有活动 ReviewRun
时可人工“重新审核”；排队或运行期间按钮禁用。外部上报按钮只有漏洞状态为“已验证”时可用，服务端仍会
执行相同门禁，不能靠前端绕过。

新建任务第二页和 stopped Task 的共享 Resume 弹窗支持“不限时/小时/分钟”。任务列表、概览和图谱共用一个秒级前端时钟展示剩余时间和本地时区截止时间；倒计时只负责显示，归零后等待服务端状态刷新，不在浏览器里擅自修改 Task。

设置页只编辑真实 `RuntimeConfig`，分为“调度并发、阶段预算、Reason 触发与派发、高级可靠性”四组；Bootstrap/Reason 单 Task 并发以只读 `1（固定）` 展示。Explore Conclude 下限默认和前后端输入约束均为 180 秒。保存使用 `expected_version` 乐观锁，合法零值不会被 `||` 回填；Runner 每 tick 读取版本并回报真实 `applied_version/applied_at`。生产浏览器已验证保存、刷新、重启持久化和下一 tick 热应用。

Artifact、Vulnerability 和 Todo 列表支持可选服务端分页、搜索和 `X-Total-Count`；未传分页参数时保持旧客户端全量行为。文件中心的“全部任务”继续使用单个跨任务 `GET /api/v1/artifacts`，每页 60 条，不再按 Task 逐个请求 Artifact。选中 Task 后改用 `GET /api/v1/tasks/{task_id}/artifact-folders` 显示 Intent 文件夹；无 `intent_id` 的上传文件归入固定的“任务文件”。进入文件夹后通过 `intent_id` 或 `unscoped_only=true` 只读取该目录的 Artifact，每页 50 条；单个 Artifact 元数据接口负责恢复深链接和跨分页定位。文件位置完整写入 `#/files?task=...&folder=...&artifact=...`，漏洞、图谱和 Human 页面统一通过 `openArtifactLocation()` 进入正确文件夹并高亮目标。

任务概览的循环卡片以唯一 `ReasonRun` 作为一轮模型，合并该轮的 Workspace 重试和 Lease，不把重试重复计数。首次加载 Audit 时读取最近 2000 条并按时间正序还原，之后用 `after_id` 游标增量轮询；同时兼容 `reason.*`、`reason_run.*` 和历史 `reason_workspace.*` 事件。当前/历史循环显示 Worker、状态、开始/结束时间、Workspace 尝试次数、创建的 Intent 和失败原因；Bootstrap、Reason、Explore 的运行/等待/完成/失败状态使用统一动画类，并遵守 `prefers-reduced-motion`。

Explore 额外提供实时对话视图。人类指导消息进入 `intent_interventions` 持久化队列，绑定当前 Explore Lease，Runner 的 RPC Supervisor 在每轮模型调用之间轮询并发送多条 `steer`。人工消息不会改变 Scope、权限、Todo/Intent 关系或阶段完成协议；Lease 结束后未发送消息过期，不会在重试中重放。对话读取 Session JSONL 的工具调用、工具结果、行动摘要和生命周期事件，并在服务端脱敏凭证、JWT、Cookie、Authorization 和 API Key，不展示隐藏思维链。

概览当前执行区按 `running/concluding > claimed > pending > completed > failed/cancelled` 选择 Intent；没有活动 Intent 时展示最近一个非空 ReasonRun 批次，不会回退到最早历史 Intent。Fact、Leads、最近 Artifact 显示来源 Intent；Fact、Leads 和产物统一复用文件中心深链，缺失或归档对象会提示而不误定位。Fact 计数排除 `goal/origin`，产物和漏洞总数使用 Task 聚合计数，Leads 单独增量读取并在没有 Explore Leads 时回退 Bootstrap 初始线索。静态资源缓存版本为 `20260803-overview-loop-v2`。

图谱右侧 Fact 详情把代码生成描述中的 `artifact:aXXX` 与 `vulnerability:vXXX` 安全拆分为普通文本和内联蓝链。普通文本始终使用 `x-text`，不执行 Fact 内容中的 HTML。Artifact 引用复用文件中心深链；漏洞引用进入 `#/vulnerabilities?task=...&vulnerability=...`，自动切换列表模式、锁定 Task，并在目标不在当前页时复用单漏洞 GET 接口补入并选中。目标不存在或已归档时会提示并清除精确目标参数，不会误选同 Task 的其它漏洞。漏洞列表每页 50 条，状态看板每列先取 25 条并独立加载更多；Human Artifact 每页 50 条，Workspace 只取最近 6 条。Intent/Fact 复用已加载 Graph 数据并限制首屏渲染。

Dashboard 使用 `GET /api/v1/dashboard/summary` 获取 Task 状态、漏洞等级、开放 Intent、Token 汇总和最近 8 条事件，不再加载完整漏洞库和大量 Audit。`TaskService._decorate_many()` 使用一条 UNION 聚合计数查询和一条活动 ReasonRun 查询，Task 数量增长不会产生 N+1。

轮询采用不可重入递归 `setTimeout`：Task Detail/Audit 为 5 秒，图谱当前活动 Intent 为 2 秒，Dashboard/Task/漏洞/文件/项目为 10 秒，静态管理页不发轮询请求。页面切换会 Abort；浏览器后台暂停；Audit `after_id` 决定需要刷新 Graph、Todo、Vulnerability 或 Artifact 的区域，每 60 秒执行一次安全刷新。全局搜索和列表搜索使用独立请求代次，旧响应不能覆盖新结果。

Cytoscape、ELK、Dagre 只在首次进入图谱页时加载，Marked 只在首次预览 Markdown 时加载。图谱更新区分 topology、geometry 和 visual：只有节点/边或尺寸变化才运行布局；状态、Worker、心跳和活动文字只更新样式。活动边共用一个 RAF，Minimap 不监听普通 data 更新，离开图谱页后全部停止和销毁。

## 9. PostgreSQL 原生数据库与迁移边界

### 9.1 当前唯一正式数据库

生产、开发和普通测试只允许连接 PostgreSQL。运行时 Engine 工厂拒绝非 PostgreSQL URL；`axiom serve` 会同时检查业务表和 Alembic Head，不会在 Schema 未升级时自动启动。API、Runner 和 Alembic 统一从 `AXIOM_DATABASE_URL`、`AXIOM_MIGRATOR_DATABASE_URL` 或 `AXIOM_DB_*` 读取连接配置。

```text
PostgreSQL 18.4 / Debian / immutable digest
host:   127.0.0.1:55432
db:     axiom
schema: axiom
owner:  axiom_owner（NOLOGIN）
migrate:axiom_migrator
runtime:axiom_app
```

结构化 JSON 使用 `JSONB`，时间使用 `TIMESTAMPTZ` 并在连接级固定 UTC。`JSONText` 与 `UTCDateTimeText` 只负责维持历史 Python/HTTP 字符串合同，不把 PostgreSQL 降级成文本存储。状态继续使用 `VARCHAR + CHECK`；字符串业务 ID 和 `id_counters` 保留。

### 9.2 并发和事务

Dispatcher 对 Intent、ReasonRun 和 ReviewRun 的领取使用 `SELECT ... FOR UPDATE SKIP LOCKED`，领取、状态更新和 Lease 创建处于同一事务。服务层对 PostgreSQL `40P01` 和 `40001` 提供最多三次带抖动重试；连接池启用 `pool_pre_ping`，并设置连接、锁和空闲事务超时。

运行时代码不再使用 `BEGIN IMMEDIATE`、PRAGMA、`sqlite_insert`、`INSERT OR IGNORE`、`sqlite_where` 或 SQLite 文件锁。冲突写入使用 PostgreSQL `ON CONFLICT`；搜索、NULL 排序和 JSON 比较使用明确的 PostgreSQL 语义。

### 9.3 Alembic 分支

SQLite 历史迁移冻结在 `20260805_0035`，不修改原有 35 个迁移。PostgreSQL 使用独立根基线：

```text
20260805_pg0001_postgresql_baseline
branch: postgresql
deploy target: postgresql@head
```

后续正式迁移只能接在 PostgreSQL 分支。空数据库已经完成 `upgrade → downgrade → upgrade`；API 启动时当前版本必须与 PostgreSQL Head 完全一致。

### 9.4 SQLite 只读导入器

SQLite 仅允许通过以下入口读取：

```bash
axiom db migrate-sqlite \
  --sqlite-path /path/to/frozen-axiom.db \
  --database-url postgresql+psycopg://... \
  --data-dir /path/to/source-data \
  --report-dir /path/to/report
```

导入器要求源端 Alembic 精确为 `20260805_0035`，以只读 URI 打开源库并执行 integrity、foreign key、JSON、时间、ID 和结构检查；目标必须已安装 PostgreSQL 基线且业务表为空。36 张业务表在一个 PostgreSQL 事务中按外键拓扑导入，任一计数、规范化 SHA-256、约束或 Counter 校验失败都会整体回滚。Artifact 文件不复制，只核对数据库元数据指向的文件是否存在。

### 9.5 生产切换和不可逆边界

生产使用 `normal → drain → frozen` 外部维护状态。`drain` 禁止新 Lease，允许已有 Lease 心跳和收口；`frozen` 只允许读取和健康检查。正式切换先冻结 SQLite、完成 WAL checkpoint 与不可变备份，再导入空 PostgreSQL、比较 API 快照并验证备份恢复。

**不可逆边界是 PostgreSQL 解除 frozen 并产生第一笔新写入。** 当前生产已经跨过该边界，因此禁止回退 SQLite；故障处理只能使用 PostgreSQL 备份恢复或修复前进。最终 SQLite 副本永久保留为只读历史输入。

本次切换报告：

```text
/Users/hexin/.local/share/axiom-v3/postgresql-migration/cutover-20260805T183453Z
```

两次完整生产副本演练、36 张业务表计数/哈希、3,547 个 Artifact 路径、SQLite/PostgreSQL API section hash、首次 `pg_dump` 恢复和四阶段真实生命周期均已通过。切换后的 24 小时观察由 `com.xrayai.axiom.postgresql-observer` 托管；观察结束前只能称“切换成功、观察中”，不能提前宣告 24 小时验收完成。

### 9.6 备份与恢复

`ops/postgres/backup.sh` 每日生成 custom-format `pg_dump`、globals SQL 和 SHA-256 manifest，保留 14 份每日与 8 份每周备份。备份含明文 API Key、Credential 和 Worker/MCP 配置，依赖 PostgreSQL与文件系统访问权限保护，不额外加密或脱敏。

`ops/postgres/restore_verify.sh` 会创建临时数据库、恢复 dump、计算全表 fingerprint、以 frozen 模式启动独立 API 并导出快照，最后删除临时数据库。生产首次备份已经通过全量 fingerprint 恢复验证。

完整操作手册见 `docs/postgresql-operations.md`；迁移记录见 `docs/2026-08-05-SQLite到PostgreSQL原生化与生产切换.md`。

## 10. 当前明确不属于主流程的内容

- Bootstrap 没有改成 ASM；ASM 工具链不是当前阶段。
- 旧爬虫后端逻辑不作为 V2 新任务入口，前端不展示。
- 每任务容器、VPN/SOCKS5 强制出口、MCP Bridge 是后续方案，不影响当前宿主机 Runner。
- 旧 `index-legacy.html` 仍保留历史整页实现和图谱脚本直载，仅用于只读对照；生产 `/` 使用按路由挂载的新入口。

## 11. 验证基线

~~~text
.venv/bin/pytest -q                           488 passed, 1 warning
.venv/bin/ruff check src tests migrations     passed
.venv/bin/python -m compileall -q src/axiom   passed
node --check <all non-vendor static js>       passed
git diff --check                              passed
~~~

当前全量测试以 PostgreSQL测试容器为唯一普通数据库，覆盖：

- PostgreSQL Engine、Schema readiness、JSONB/TIMESTAMPTZ兼容层和事务重试；
- Intent、ReasonRun、ReviewRun 的行锁领取、唯一性与 Lease 恢复；
- Bootstrap、Reason、Explore、Reviewer 四阶段完整生命周期；
- Reason Workspace、Goal Cycle、Todo Review、Context Revision、Archive；
- Task 运行窗口、Task 级归档、Fact/Intent 链路归档及披露隔离；
- Token Usage、MCP Tool Schema 顺序、阶段能力快照和原生浏览器 Lease；
- SQLite历史导入器的正常导入、空目标保护、失败回滚、计数/哈希/Counter/Artifact校验；
- PostgreSQL Alembic空库升级、降级和再次升级；
- API健康检查与数据库 revision 门禁。

迁移实证：

~~~text
生产副本演练 1：PASS
生产副本演练 2：PASS
正式 36 表导入：PASS
Artifact 3547/3547：PASS
SQLite/PostgreSQL API section hash：PASS
首次 pg_dump 恢复 fingerprint：PASS
生产四阶段生命周期 15 项：PASS
24 小时观察：运行中，等待 summary.json passed=true
~~~

唯一 warning 是既有 Starlette/httpx 弃用提示。生产已经开放 PostgreSQL 写入，SQLite 回滚被永久禁止。
