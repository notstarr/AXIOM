# AXIOM 当前生产架构

> 状态：已核对至 2026-08-03。本文描述当前实际代码，不描述未落地的 ASM、容器化或爬虫方案。

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
 ├─ archives       Fact/Intent 下游链路的批次归档与恢复
 ├─ vulnerabilities 漏洞对象、状态、导出
 ├─ external_reports 外部上报与重试记录
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
```

Task 完成的唯一业务指标是 Goal Evaluation。没有 Intent、没有 Worker 或某一轮 Reason 结束都不能单独表示任务完成。

新建和 Resume 可提供整分钟 `run_duration_seconds`；省略或 `null` 表示不限时。每次 Resume 都覆盖旧窗口，按自然时间计算 `run_deadline_at`，排队、Reason 间隔和重试都计入。到期由 Runner 下一 tick 调用 Deadline Reconciler 原子暂停，默认精度约 3 秒；服务中断期间过期的 Task 会在恢复后的首个 tick 暂停。

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

统一工作视图同时包含 Bootstrap/Explore Intent 和 ReasonRun，Task 内优先级为 `Bootstrap → Reason → Explore`；Task 之间按最近一次领取时间轮询，每轮优先给每个 Task 一个工作，避免低 ID Task 长期占满槽位。ReasonRun 占用全局和单 Task Run 槽位，但不计入 Intent 数量或 Explore 并发。

有效领取会同时校验 `global_max_active_runs`、`global_max_active_tasks`、`per_task_max_active_runs`、`per_task_explore_concurrency`、Worker `max_running`、能力和 Task Worker Pool。纯调度计算先过滤一次，数据库 claim 事务再校验一次，防止多个 tick/Runner 并发越限。Bootstrap 和 Reason 的单 Task 并发固定为 1。

Claim 事务和 Reason Trigger 还会校验 Task 的绝对 UTC 截止时间，防止 Deadline Reconciler 与领取之间的竞争窗口产生新工作。多个 Runner 同时发现到期 Task 时，只有命中 `status=running AND run_deadline_at<=now` 条件更新的实例能写入暂停状态和 Audit Event。

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

Chrome PID 扫描会排除带 `--type=` 的 Renderer/Helper，只登记专用 Profile 和 CDP 端口匹配的主进程；停止时按该主进程的 PGID 清理整个浏览器进程组。`cdp_ctl.py` 对连接设置 10 秒上限，`url/title` 走轻量 CDP HTTP；新建 Tab 写入 `/tmp/axiom-cdp-tabs-<port>.json`，记录 Lease、Task、Intent、别名和活动时间。别名只在当前 Lease 内解析，Runner 在每个 Pi 阶段结束后调用 `close-lease-tabs`，不会因不同 Task 复用 `i001` 等别名而关闭其它 Lease 页面。

Browser-control 的 MITM 代理由 `launch-proxy.sh/stop-proxy.sh` 独立管理。代理进程使用环境白名单启动，不继承 `PI_API_KEY`、Task/Intent/Lease 标识或其它 Worker 私密环境；标准流均为 `DEVNULL` 且使用独立 Session。流量 JSONL 默认每 64 MiB 轮转并保留 3 个分片，addon 通过文件锁避免多实例竞争；读取器从文件尾反向分块读取并覆盖轮转分片，`--status` 不再扫描或加载数 GiB 历史日志。共享 Chrome 请求 MITM 模式时会先确认代理就绪；已经以无代理模式运行的 Chrome 必须安全停止后重启，禁止热切换。

Execute 超时后，Explore Conclude 使用同一 Pi session ID 启动新的 RPC 进程继续有界收口，但不会再次发送 70% 提醒。Explore 收口预算的正式下限为 180 秒、上限默认为 600 秒；Reason 提交低于下限的 Intent 会被 API 明确拒绝，不做静默修正。

### Token 用量持久化

Pi Session JSONL 是实时和历史 Token 用量的主要事实源。Session tailer 在 Bootstrap、Reason、Explore 的 JSON/RPC 执行中共用，解析 assistant message、toolResult、compaction 和 branch summary 中明确报告的 usage；不从文本长度估算，也不递归读取 `retainedTail`。明细写入 `llm_token_usage`，以 `session_file + session_id + session_entry_id + usage_kind` 的稳定键幂等去重；HTTP 上报失败只写入当前 Lease 的 `process/token-usage.pending.jsonl`，不会让阶段失败。

每条用量保留任务、Intent/ReasonRun、Lease、阶段、Worker、provider、实际响应模型、Session entry 和四类 Token：未命中输入、缓存命中、缓存写入、输出。总量为四类之和，`reasoning` 是输出子集，不重复加入；缓存命中率为 `cacheRead / (input + cacheRead + cacheWrite)`，分母为零时为空。`llm_token_usage_sources` 记录 Session 是否完整；路径缺失、JSONL 截断、字段无法恢复的历史来源标记为 `incomplete`，不伪造缺失数量。

Dashboard 和任务 Diagnostics 通过已有汇总接口支持时间范围和实际 `provider/model` 筛选，任务明细覆盖所有 Goal Cycle、成功、失败、超时、重试、Execute 和 Conclude。`axiom token-usage-backfill` 扫描 `runs/**/sessions/*.jsonl` 并复用同一幂等写入服务；旧 Audit Event 的 Token 只作为标记为 `legacy/incomplete` 的兼容来源，已有 Session 记录时不重复导入。

2026-07-27 的生产受控验收确认了两种路径：60 秒 Execute 在约第 42 秒产生 `soft_reminder`；提醒若落在工具调用中，会由 Pi 排队并在工具结束后的下一轮模型调用前注入。验收会话中该 steer 作为 user 消息落入 session，模型随后仅返回 `FINISH`，Intent 在 49.7 秒内完成且没有进入 Conclude 或重试。另一轮故意让慢请求撞上 Execute 硬超时，验证了同 session RPC Conclude 可继续整理并原子提交 Artifact/Fact。

后续 `t_147` 生产受控验收专门覆盖 Pi 自身重试：本机兼容服务第一次返回 HTTP 500，Pi 自动发起第二次请求；动态 70% steer 被接受后触发第三次模型调用。最终 RPC 日志包含 2 次 `agent_end`、1 次 `auto_retry_start/end`、1 次 `agent_settled`，随后 `get_last_assistant_text` 返回精确 `FINISH`，代码成功注册 `explore.findings`、`explore.leads` 并生成 Fact。临时 Task、Project、Worker、服务和文件随后全部清理，正式 Runner 保持运行。

2026-07-28 在生产 `t_148` 上验证了日志与浏览器修复：共享 Chrome-SecTest 以 `PPID=1`、独立 PGID 和三个 `/dev/null` 标准 FD 运行，管道包裹的启动命令可立即返回；新 RPC Lease 不再记录三类累计流式事件。`i014` 完成后的诊断日志为 47,107 B、Session 为 432,577 B，EOF 摘要记录省略 1,135 条 `message_update` 和 7 条 `tool_execution_update`，仍正常经过 `agent_settled → get_last_assistant_text(FINISH)` 完成 Intent 并生成 Fact。

### 阶段完成协议

三个阶段的 Prompt 都要求最后一条 assistant 文本只包含：

```text
FINISH
```

Bootstrap 和 Reason 仍严格匹配：工具输出、代码块、`FINISH` 前后附加说明或大小写变化均不算完成。Explore Execute 使用有限归一化，可接受无害 Markdown 包装或前置说明后以独立 FINISH 结尾；非精确通过会记录 warning。

`FINISH` 通常只触发产物/Workspace 校验，不直接改状态；缺失项会在同一 Pi 会话和共享 conclude deadline 内最多收口 3 次。Explore Conclude 是唯一例外：收口最终没有 FINISH、格式错误或超时时，只要 `findings.md` 与 `leads.md` 已有效落地，代码就以核心文件为完成依据继续原子提交。Evidence 永远不能替代两个核心文件，也不再成为 Explore 成功门槛。Bootstrap、Reason 没有该兜底。

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
 ├─ goal_evaluations.json   # Bootstrap / Explore
 └─ goal_cycles.json        # Bootstrap / Explore
```

Fact 和 Artifact 仍只保留 preview 与 detail ref；`todos.json` 额外提供完整 `title/status_reason`，`graph.json` 的 Intent 摘要额外提供 `todo_id`，使 Reason 能先做历史主题对照，再只展开相似 Todo/Intent。Reason phase 的 Manifest 不包含 `goal_evaluations` 与 `goal_cycles` 索引，也不会生成这两个文件；当前 Goal 仍直接存在于 Manifest，续跑原因/重点由 ExecutionInput 注入。Bootstrap/Explore 保持历史索引兼容。Worker 仍通过 detail 文件或 Context expand API 获取长内容，不会把整个图谱和产物正文重复塞进上下文。

新 Context Revision 会排除已归档 Fact、Intent、Todo、Artifact 和 Vulnerability；Intent 的来源/产出、Todo source、Artifact/Vulnerability 的 `intent_id` 也会移除指向归档实体的引用。引用归档证据的历史 Goal Evaluation 不进入 Worker Context，`/context/expand` 对归档实体返回不存在。已有 Lease 的不可变快照不回写，下一 Lease 才应用新的可见集合。

## 7. 文件和产物

FileStore 使用外部 data-dir，Artifact 表只保存元数据、hash、size 和 storage key；文件内容仍进入现有文件管理体系。任务删除、Artifact 归档和迁移均不允许通过路径穿越访问外部文件。

阶段文件采用 prepare → 文件提交 → 数据库事务 → finish 的收口顺序。数据库事务成功后删除 Worker 运行目录中的原文件，正式 Artifact 内容只保留一份；事务失败则回滚暂存文件并保留运行目录证据供同 lease 收口重试。内部索引、Pi session、进程日志和模型配置永远不会被自动注册为 Artifact。

项目凭证、Task Hints、Project Knowledge 分别保存；凭证按阶段执行输入按需提供，不混入 Origin，也不写入 Fact、Artifact 摘要或命令参数。

### 链路级联归档

Fact/Intent 归档使用 `archive_batches + archive_batch_members`，不是直接翻转单条 `archived`。Fact 根会包含其生产 Intent、自身和所有下游 Intent/Fact；Intent 根会包含自身、产出 Fact 和后续下游，但永远不反向归档来源 Fact 或旁支。闭包内 Intent 的 Artifact、Vulnerability 和漏洞证据 Artifact 一并归档；Todo 仅在没有其它可见关联 Intent 时归档。Bootstrap Intent、Bootstrap 产出 Fact、`origin` 和 `goal` 受保护，活动 Intent 会阻塞归档。

预览返回 `closure_hash`，提交在 SQLite `BEGIN IMMEDIATE` 中重算；闭包变化返回 409。恢复按批次执行，仍被其它 active 批次覆盖的成员继续保持归档。归档不删除文件、Session、Audit Event 或历史 Context；恢复也不改写 Intent/Todo/漏洞原业务状态。若归档与活动 ReasonRun 的 source 或 open Workspace 引用相交，ReasonRun 会被取消且 Workspace 被废弃；无关 Explore 不受影响。

## 8. 前端

前端继续使用 Alpine 和原有图谱视觉，通过 V2 API 适配层对接模块化后端，不引入 React/Vue。Tailwind 固定为 `3.4.17` 并在开发时编译为 `tailwind.generated.css`；生产浏览器不再加载 Tailwind Runtime，也不依赖 Node。动态状态类通过显式 safelist 进入构建产物，连续构建必须产生相同文件。

桌面侧栏默认宽 176px，显示图标和文字；可折叠为 56px 纯图标模式并写入 `localStorage`。移动端使用汉堡按钮和抽屉导航，选择页面后自动关闭。主内容宽度由侧栏状态联动，不使用固定 margin。

页面使用单一 `#page-host` 按路由挂载。切换页面会 Abort 旧请求、执行 `Alpine.destroyTree()`、销毁 Cytoscape、Minimap、共享图谱动画和旧 DOM；模板文本只做内存缓存，未激活的 13 个页面不再常驻 DOM。任务详情 Human Tab 使用 `x-if`，只渲染当前 Workspace/Todo/Intent/Fact/Artifact 面板。

任务详情提供“概览 / 图谱 / 归档”三种视图；归档视图按批次展示根节点、原因、操作者、成员类型、关系、Artifact 预览/下载和恢复历史。Fact/Intent 归档先展示闭包、阻塞项和影响数量，再原子提交；停止或完成 Task 仍可整理历史链路。

任务详情的“概览”和“图谱”是两种人类视图：图谱不是执行事实源；右侧 Todo、事实、产物和漏洞分别从 V2 API 读取。ReasonRun 只显示为图谱上方的运行活动卡片，可查看 Worker、状态、当前活动、工具、心跳、触发器、预算和配置快照，不创建图谱节点。

新建任务第二页和 stopped Task 的共享 Resume 弹窗支持“不限时/小时/分钟”。任务列表、概览和图谱共用一个秒级前端时钟展示剩余时间和本地时区截止时间；倒计时只负责显示，归零后等待服务端状态刷新，不在浏览器里擅自修改 Task。

设置页只编辑真实 `RuntimeConfig`，分为“调度并发、阶段预算、Reason 触发与派发、高级可靠性”四组；Bootstrap/Reason 单 Task 并发以只读 `1（固定）` 展示。Explore Conclude 下限默认和前后端输入约束均为 180 秒。保存使用 `expected_version` 乐观锁，合法零值不会被 `||` 回填；Runner 每 tick 读取版本并回报真实 `applied_version/applied_at`。生产浏览器已验证保存、刷新、重启持久化和下一 tick 热应用。

Artifact、Vulnerability 和 Todo 列表支持可选服务端分页、搜索和 `X-Total-Count`；未传分页参数时保持旧客户端全量行为。文件中心的“全部任务”继续使用单个跨任务 `GET /api/v1/artifacts`，每页 60 条，不再按 Task 逐个请求 Artifact。选中 Task 后改用 `GET /api/v1/tasks/{task_id}/artifact-folders` 显示 Intent 文件夹；无 `intent_id` 的上传文件归入固定的“任务文件”。进入文件夹后通过 `intent_id` 或 `unscoped_only=true` 只读取该目录的 Artifact，每页 50 条；单个 Artifact 元数据接口负责恢复深链接和跨分页定位。文件位置完整写入 `#/files?task=...&folder=...&artifact=...`，漏洞、图谱和 Human 页面统一通过 `openArtifactLocation()` 进入正确文件夹并高亮目标。

任务概览的循环卡片以唯一 `ReasonRun` 作为一轮模型，合并该轮的 Workspace 重试和 Lease，不把重试重复计数。首次加载 Audit 时读取最近 2000 条并按时间正序还原，之后用 `after_id` 游标增量轮询；同时兼容 `reason.*`、`reason_run.*` 和历史 `reason_workspace.*` 事件。当前/历史循环显示 Worker、状态、开始/结束时间、Workspace 尝试次数、创建的 Intent 和失败原因；Bootstrap、Reason、Explore 的运行/等待/完成/失败状态使用统一动画类，并遵守 `prefers-reduced-motion`。

概览当前执行区按 `running/concluding > claimed > pending > completed > failed/cancelled` 选择 Intent；没有活动 Intent 时展示最近一个非空 ReasonRun 批次，不会回退到最早历史 Intent。Fact、Leads、最近 Artifact 显示来源 Intent；Fact、Leads 和产物统一复用文件中心深链，缺失或归档对象会提示而不误定位。Fact 计数排除 `goal/origin`，产物和漏洞总数使用 Task 聚合计数，Leads 单独增量读取并在没有 Explore Leads 时回退 Bootstrap 初始线索。静态资源缓存版本为 `20260803-overview-loop-v2`。

图谱右侧 Fact 详情把代码生成描述中的 `artifact:aXXX` 与 `vulnerability:vXXX` 安全拆分为普通文本和内联蓝链。普通文本始终使用 `x-text`，不执行 Fact 内容中的 HTML。Artifact 引用复用文件中心深链；漏洞引用进入 `#/vulnerabilities?task=...&vulnerability=...`，自动切换列表模式、锁定 Task，并在目标不在当前页时复用单漏洞 GET 接口补入并选中。目标不存在或已归档时会提示并清除精确目标参数，不会误选同 Task 的其它漏洞。漏洞列表每页 50 条，状态看板每列先取 25 条并独立加载更多；Human Artifact 每页 50 条，Workspace 只取最近 6 条。Intent/Fact 复用已加载 Graph 数据并限制首屏渲染。

Dashboard 使用 `GET /api/v1/dashboard/summary` 获取 Task 状态、漏洞等级、开放 Intent、Token 汇总和最近 8 条事件，不再加载完整漏洞库和大量 Audit。`TaskService._decorate_many()` 使用一条 UNION 聚合计数查询和一条活动 ReasonRun 查询，Task 数量增长不会产生 N+1。

轮询采用不可重入递归 `setTimeout`：Task Detail/Audit 为 5 秒，图谱当前活动 Intent 为 2 秒，Dashboard/Task/漏洞/文件/项目为 10 秒，静态管理页不发轮询请求。页面切换会 Abort；浏览器后台暂停；Audit `after_id` 决定需要刷新 Graph、Todo、Vulnerability 或 Artifact 的区域，每 60 秒执行一次安全刷新。全局搜索和列表搜索使用独立请求代次，旧响应不能覆盖新结果。

Cytoscape、ELK、Dagre 只在首次进入图谱页时加载，Marked 只在首次预览 Markdown 时加载。图谱更新区分 topology、geometry 和 visual：只有节点/边或尺寸变化才运行布局；状态、Worker、心跳和活动文字只更新样式。活动边共用一个 RAF，Minimap 不监听普通 data 更新，离开图谱页后全部停止和销毁。

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

`20260727_0023_explore_closeout_floor` 是单调数据迁移：把 RuntimeConfig 的 Explore Conclude 下限、未结束 Explore Intent 的收口预算，以及未结束 ReasonRun 的 Explore 收口范围快照提升到至少 180 秒；必要时同步抬高过低的 max。已进入进程的旧 ExecutionInput 不会被热改，下一次重试或重新领取才使用数据库中的新预算。降级保留已提升的安全预算，避免误改用户在升级后主动设置的 180 秒值。

`20260728_0024_task_run_windows` 为 Task 增加本次运行时长、开始时间、UTC 截止时间和最近暂停原因，并建立 `status + run_deadline_at` 索引。历史记录保持 `NULL`，等价于不限时，不猜测旧任务的运行窗口。

`20260729_0025_archive_batches` 新增归档批次和成员表。迁移前已归档的 Fact、Intent、Artifact、Vulnerability、Todo 会分别生成兼容批次；Todo 使用真实 `root_type=todo` 仅用于历史读取，新的交互式归档根仍限制为 Fact、Intent、Artifact 或 Vulnerability。降级只删除批次索引，不修改原实体的 `archived` 状态。

`20260729_0026_frontend_query_indexes` 为 Artifact 和 Vulnerability 的分页、过滤和稳定倒序读取增加组合索引。迁移不改业务数据；生产副本已完成 `0026 → 0025 → 0026`，SQLite integrity 为 `ok`。

`20260729_0027_artifact_folder_index` 为 Task 内按 Intent 聚合和读取 Artifact 增加 `(task_id, archived, intent_id, created_at)` 索引。迁移不修改业务数据；生产副本完成 `0026 → 0027 → 0026 → 0027`，SQLite integrity 为 `ok`。

`20260803_0029_llm_token_usage` 新增 Session Token 明细和来源完整性表，以及任务/模型/时间索引。迁移不改写历史 Session 或 Audit Event；生产切换时先升级数据库，再使用 `axiom token-usage-backfill --dry-run` 检查可识别 Lease、合法 usage 记录和不完整来源，确认报告后再正式回填。回填和实时上报共用稳定键，不会因 Runner 重启或重复扫描重复计数。

2026-07-29 生产升级前备份位于 `/Users/hexin/.local/share/axiom-v3/backups/archive-batches-20260729T034510Z/axiom.db.pre-0025.bak`。截至本文状态，生产库 revision 为 `20260729_0028`；`20260803_0029_llm_token_usage` 尚未迁移。API 与 Runner LaunchAgent 均正常运行，SQLite integrity 为 `ok` 且 foreign key violation 为 0。

## 10. 当前明确不属于主流程的内容

- Bootstrap 没有改成 ASM；ASM 工具链不是当前阶段。
- 旧爬虫后端逻辑不作为 V2 新任务入口，前端不展示。
- 每任务容器、VPN/SOCKS5 强制出口、MCP Bridge 是后续方案，不影响当前宿主机 Runner。
- 旧 `index-legacy.html` 仍保留历史整页实现和图谱脚本直载，仅用于只读对照；生产 `/` 使用按路由挂载的新入口。

## 11. 验证基线

```text
uv run pytest                            406 passed, 1 warning
uv run ruff check .                      passed
python -m compileall -q src tests migrations  passed
node --check <18 non-vendor static js>   passed
git diff --check                         passed
```

以上测试已覆盖 FINISH 精确识别与 Explore 有限归一化、同会话有界收口、Explore Conclude 核心文件兜底、可选 Evidence、Artifact/Fact 自动生成、ReasonRun/Workspace 生命周期、Goal Cycle 续跑、Reason 历史 Goal Context 隔离、无新 Fact 的材料性 Todo 恢复、Reason 触发和调度并发、RuntimeConfig 热应用、Guard、Task 运行窗口、归档下游闭包，以及前端分页、Dashboard 聚合、Task 固定查询数、搜索竞态、历史 Goal 详情、任务概览循环还原与状态动画、Fact/Leads/Artifact 来源和深链、图谱 Fact 安全引用/漏洞跨分页深链、图谱纯视觉更新不触发布局和 Tailwind 可重复构建；仅有 Starlette/httpx 弃用警告。

2026-07-29 前端生产数据浏览器验收：Dashboard/Task/Files/Vulnerability 非图谱页面分别为 592/1217/1258/839 个 DOM 节点；文件中心首屏 60 条、漏洞列表 50/565；非图谱冷启动没有图谱 Runtime，进入图谱后才加载 6 个依赖，离开后 Canvas 和图谱 DOM 清零；桌面侧栏 176→56px 且刷新持久化，390×844 移动端抽屉导航后自动关闭，控制台无 warning/error。生产数据库副本上的 `/api/v1/tasks` 60 次请求为 median 19.14ms、P95 28.71ms、max 36.16ms。

2026-07-27 生产受控 E2E `t_138` 完成 `Bootstrap → ReasonRun(rr000001) → Explore → ReasonRun(rr000002) → Goal`：正式图谱只有 Bootstrap/Explore 两个 Intent，两个 ReasonRun 均零重试完成；生成 4 个 Artifact、1 个 completed Todo 和两个代码生成 Fact，最终 `goal_from_fact_ids=[f002]`。Task completed 后开放 Intent、ReasonRun、Workspace 均为 0。

最终重启后又执行 API-only 临时验收 `t_141`（随后删除）：Bootstrap 完成后创建独立 ReasonRun，图谱仍只有 Bootstrap Intent；打开 Workspace 后停止 Task，ReasonRun 立即 `cancelled`、Workspace 变为 `abandoned(task_stopped)`、旧 heartbeat 和 commit 均返回 409。临时 Task/Project/Worker 已清理，生产 RuntimeConfig 保存/应用版本均为 v210，活动 Intent、ReasonRun、open Workspace 均为 0。浏览器再次确认 `t_138` 图谱只有 2 个正式 Intent，设置页四组配置和 `Runner 已应用 v210` 正常显示；当时控制台仅保留既知 Tailwind CDN 警告。该警告已在 2026-07-29 静态 Tailwind 构建改造后消失。

Explore RPC/动态提醒批次的生产一致性备份为 `/Users/hexin/.local/share/axiom-v3/backups/20260727-182223/`。空库及生产副本完成 `0022 → 0023 → 0022 → 0023`，生产库现为 `20260727_0023`，RuntimeConfig/Runner applied version 均为 v212，Explore Conclude 范围为 180–600 秒，SQLite integrity 为 `ok` 且 foreign key violation 为 0。临时生产验收 `t_146` 的两个 Explore Intent 分别验证“Execute 超时后 RPC Conclude 成功提交”和“工具调用期间 steer 排队后下一轮生效”；共生成 9 个正式 Artifact、2 个代码生成 Fact，执行/基础设施重试均为 0，临时 Task、Project、本机服务和 Pi 进程随后清理。

2026-07-28 部署 Explore 核心产物优先协议时，先停止唯一运行任务 `t_150`，确认 Dispatcher 活动工作为 0，再重启 `com.xrayai.axiom` 与 `com.xrayai.axiom.runner` 并恢复任务。恢复后的 `rr000096` 正常提交 4 个 Explore Intent；新 Lease 的 Pi Session 已确认包含可选 Evidence、未完成写入 Leads 和“不要补跑”的新 Prompt。`i035-i037` 均一次完成且执行/基础设施重试为 0：`i035` 生成 2 个核心 Artifact、3 个 Evidence 和 5 个待验证漏洞，其中一个漏洞只有 Findings 关系、Evidence 为空；`i036` 只生成 findings/leads 两个核心 Artifact，也正常完成；`i037` 在 600 秒 Execute 的动态 70% 时点收到 steer，生成 2 个核心 Artifact、8 个 Evidence 和 `f035`。这些真实路径验证了可选 Evidence、无 Evidence 漏洞和常规 RPC 收口；缺少 FINISH/收口超时的核心文件兜底由自动化模拟测试覆盖。

2026-07-29 归档生产 smoke 使用临时 `t_152` 完成 `Bootstrap → ReasonRun → Explore → Fact 根归档 → Context/Graph/Artifact 隔离 → 归档 Artifact 读取 → 批次恢复`。实际闭包包含 1 Fact、1 Intent、1 Todo 和 3 Artifact，恢复 6 项；`/context/expand` 对归档实体返回 404。临时 Task、Project、Worker 和文件均已清理，生产归档批次表恢复为 0 条。浏览器在 `t_150/f100` 上只执行只读预览，确认显示 2 Fact、2 Intent、2 Todo 和 54 Artifact；同时修复空 `blockers` 长度值 `0` 被 Alpine 作为 HTML boolean attribute 导致确认按钮错误禁用的问题，修复后按钮可用且未提交真实归档。
