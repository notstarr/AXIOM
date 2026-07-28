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

Reason 最后一条 assistant 文本精确等于 `FINISH` 后，Runner 先读取 Workspace readiness：必须存在 Goal assessment，并且要么继续派发工作，要么用真实 Fact 证据证明 Goal 已达成。校验通过才调用 `/commit`。提交后由代码生成 `reason_plan.json` 审计文件；`/submit` 不再存在。

ReasonRun 创建时还会快照 `todo_review_ids`：这些 Todo 处于 pending/active、至少有一个终态 Explore，且已经没有开放 Explore。Reason 必须逐个明确收口为 completed/rejected/obsolete；若保持 active，必须填写原因并在同一 Workspace 为该 Todo 提交新的 Intent。readiness 会拒绝遗漏审查、空 active，以及“终态 Todo 同时派发 Intent”。审查集合固定在 ReasonRun 快照中，不会因并发 Explore 在执行期间结束而动态扩大。

同一轮新增 Todo 可以使用临时 `ref` 被 Intent 或 Goal Assessment 引用。提交事务先创建 Todo，再将临时引用解析为真实 `tdXXX`；引用不存在时整批回滚，不会留下半套规划或错误的 Goal Evaluation。

Reason Trigger Reconciler 在 Bootstrap/Explore 完成后立即评估，并在每个 Runner tick 周期评估。前置条件是 Task running、Bootstrap 已完成、Goal 未达成、没有活动 ReasonRun，且除初次外已满足最短间隔。满足以下任一条件时创建新 ReasonRun：未规划 Fact 达到批量数；开放 Explore 低于低水位且存在新 Fact、失败终态或未派发的可执行 Todo；最老未规划 Fact 达到最大等待。输入状态指纹防止同一低水位状态空转，Task 恢复只重新评估规则，不无条件触发。

输入指纹去重同时覆盖 `completed` 和重试耗尽后的 `failed` ReasonRun，避免相同终态输入在每个 tick 被无限重建；人工停止产生的 `cancelled` 不参与去重，因此恢复 Task 后仍可按当前事实重新评估。

Goal 未达成时，Workspace 必须已有开放 Explore 或至少提交一个 proposal；Goal 达成时禁止 proposal，并要求至少一个真实 `fact:<id>` 证据。完成事务创建 Goal Evaluation、取消其它开放 Intent/ReasonRun 并结束 Task。新图谱不创建 Reason 节点，最新 Goal 证据直接显示为 `Fact(s) → Goal`；历史 Reason Intent 保持只读原样。

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

漏洞表保存标题、类型、等级、目标、描述、前置条件、影响、修复建议、状态和来源元数据；详细发现只写入 findings，原始材料只保存在 evidence。`vulnerability_artifacts` 使用 `finding/evidence` 角色关联正式 Artifact，新漏洞不再写 `reproduction_steps`、`curl_command`、自由文本 `evidence` 或 `source_file`。旧漏洞保留 V1 列并通过只读 `legacy` 区域展示，不批量复制文件。

Explore Fact 由代码使用原 Intent description、findings/leads Artifact ID、证据数量和本轮 Vulnerability ID 生成，不使用模型尾部文本。`explore_result.json` 只是代码维护的内部状态/索引，不要求 Worker 编写，也不注册为 Artifact。

Explore 是当前唯一使用 Pi RPC 模式的阶段。Runner 通过 stdin/stdout JSONL 发送初始 `prompt` 并持续读取 Pi 活动事件；当本 Intent 的 Execute 预算消耗到动态 70% 时发送一次 `steer`，例如 600/1200/1800 秒分别在 420/840/1260 秒提醒。提醒要求停止扩展新方向、立即整理 findings/leads、把未完成分支如实写入 leads，并明确禁止为了补 Evidence 重跑大型测试；它只进入模型上下文，不延长硬超时、不改变 lease，也不改变工具权限。若阶段已提前结束则不发送；发送或 Activity 上报失败只记录警告。Task 停止、lease 失效或硬超时时，RPC Supervisor 发送 `abort` 并清理整个进程组。

Pi RPC 的会话完成边界是 `agent_settled`，不是 `agent_end`。一次会话可以依次出现多个 `agent_end`，其后仍可能发生 Pi 内部自动重试、Compaction 或处理排队的 steer；Supervisor 因此只把 `agent_end` 当作单轮活动事件。收到 `agent_settled` 后，Supervisor 才发送 `{"id":"axiom-final-text","type":"get_last_assistant_text"}`，并把成功响应中的 `data.text` 作为唯一权威最终文本。settled 前 EOF、最终文本响应前 EOF、查询失败或缺失文本均属于 RPC 协议错误；Pi 的真实非零退出码不会被完成事件覆盖成 0。

`process/stdout.log` 在 RPC 模式下是过滤后的协议诊断日志，不再是完整事件镜像。Pi 的 `message_update`、`tool_execution_update` 和 `bash_execution_update` 都会携带累计内容，逐行原样保存会使日志随生成长度近似 O(n²) 增长；Reader 因此只保留响应、生命周期、重试、Compaction、工具开始/结束等诊断事件，并在 EOF 追加 `axiom_rpc_log_summary` 记录各类省略数量。控制队列也只接收 Supervisor 真正需要的 `response`、`agent_settled` 和 EOF。完整消息、Tool Call、Tool Result 与流式历史仍以 Pi Session JSONL 为唯一权威审计源，实时活动也继续增量读取 Session，不依赖被过滤的进程日志。

生产 Pi 运行时固定在忽略版本控制的 `runtime/pi/0.82.1/`，启动器 `runtime/pi/0.82.1/bin/pi` 明确使用 `/Users/hexin/.workbuddy/binaries/node/versions/22.22.2/bin/node`，不依赖 `/usr/local/bin/pi` 的 shebang 或 LaunchAgent PATH。所有 Pi Worker 的既有 `config.binary` 均指向该启动器；Bootstrap、Reason 只升级运行时，仍使用 JSON 模式。

共享 Chrome-SecTest 仍是跨 Lease 持久化的浏览器，不归任何单个 Worker 进程组管理。`launch-chrome.sh` 通过 Python `subprocess.Popen` 启动 Chrome，并显式使用 `DEVNULL` 关闭标准输入、输出和错误，设置 `start_new_session=True` 与 `close_fds=True`；因此即使命令被 `head`/`tail` 等管道包裹，Tool Call 也会在 CDP 就绪后正常收到 EOF，Chrome 则继续独立存活。CDP 就绪后脚本重新按专用 Profile 与端口扫描实际 Chrome PID 再写 PID 文件；启动失败只清理同时匹配这两个条件的进程，不操作个人 Chrome。`CHROME_BIN` 仅作为测试或非标准安装路径覆盖。

Execute 超时后，Explore Conclude 使用同一 Pi session ID 启动新的 RPC 进程继续有界收口，但不会再次发送 70% 提醒。Explore 收口预算的正式下限为 180 秒、上限默认为 600 秒；Reason 提交低于下限的 Intent 会被 API 明确拒绝，不做静默修正。

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

新建任务第二页和 stopped Task 的共享 Resume 弹窗支持“不限时/小时/分钟”。任务列表、概览和图谱共用一个秒级前端时钟展示剩余时间和本地时区截止时间；倒计时只负责显示，归零后等待服务端状态刷新，不在浏览器里擅自修改 Task。

设置页只编辑真实 `RuntimeConfig`，分为“调度并发、阶段预算、Reason 触发与派发、高级可靠性”四组；Bootstrap/Reason 单 Task 并发以只读 `1（固定）` 展示。Explore Conclude 下限默认和前后端输入约束均为 180 秒。保存使用 `expected_version` 乐观锁，合法零值不会被 `||` 回填；Runner 每 tick 读取版本并回报真实 `applied_version/applied_at`。生产浏览器已验证保存、刷新、重启持久化和下一 tick 热应用。

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

## 10. 当前明确不属于主流程的内容

- Bootstrap 没有改成 ASM；ASM 工具链不是当前阶段。
- 旧爬虫后端逻辑不作为 V2 新任务入口，前端不展示。
- 每任务容器、VPN/SOCKS5 强制出口、MCP Bridge 是后续方案，不影响当前宿主机 Runner。
- `cdn.tailwindcss.com` 仍产生浏览器生产警告，但不影响业务功能；后续可替换为构建产物。

## 11. 验证基线

```text
uv run pytest                            294 passed, 1 warning
uv run ruff check .                      passed
python -m compileall -q src tests migrations  passed
node --check <18 non-vendor static js>   passed
git diff --check                         passed
```

以上测试已覆盖 FINISH 精确识别与 Explore 有限归一化、同会话有界收口、Explore Conclude 核心文件兜底、可选 Evidence、Artifact/Fact 自动生成、ReasonRun/Workspace 生命周期、Reason 触发和调度并发、RuntimeConfig 热应用、Guard，以及 Task 不限时/限时创建、Resume 新窗口、到期暂停、并发幂等、Reason/Claim 截止守卫和前端共享倒计时；仅有 Starlette/httpx 弃用警告。

2026-07-27 生产受控 E2E `t_138` 完成 `Bootstrap → ReasonRun(rr000001) → Explore → ReasonRun(rr000002) → Goal`：正式图谱只有 Bootstrap/Explore 两个 Intent，两个 ReasonRun 均零重试完成；生成 4 个 Artifact、1 个 completed Todo 和两个代码生成 Fact，最终 `goal_from_fact_ids=[f002]`。Task completed 后开放 Intent、ReasonRun、Workspace 均为 0。

最终重启后又执行 API-only 临时验收 `t_141`（随后删除）：Bootstrap 完成后创建独立 ReasonRun，图谱仍只有 Bootstrap Intent；打开 Workspace 后停止 Task，ReasonRun 立即 `cancelled`、Workspace 变为 `abandoned(task_stopped)`、旧 heartbeat 和 commit 均返回 409。临时 Task/Project/Worker 已清理，生产 RuntimeConfig 保存/应用版本均为 v210，活动 Intent、ReasonRun、open Workspace 均为 0。浏览器再次确认 `t_138` 图谱只有 2 个正式 Intent，设置页四组配置和 `Runner 已应用 v210` 正常显示；控制台仅保留既知 Tailwind CDN 警告。

Explore RPC/动态提醒批次的生产一致性备份为 `/Users/hexin/.local/share/axiom-v3/backups/20260727-182223/`。空库及生产副本完成 `0022 → 0023 → 0022 → 0023`，生产库现为 `20260727_0023`，RuntimeConfig/Runner applied version 均为 v212，Explore Conclude 范围为 180–600 秒，SQLite integrity 为 `ok` 且 foreign key violation 为 0。临时生产验收 `t_146` 的两个 Explore Intent 分别验证“Execute 超时后 RPC Conclude 成功提交”和“工具调用期间 steer 排队后下一轮生效”；共生成 9 个正式 Artifact、2 个代码生成 Fact，执行/基础设施重试均为 0，临时 Task、Project、本机服务和 Pi 进程随后清理。

2026-07-28 部署 Explore 核心产物优先协议时，先停止唯一运行任务 `t_150`，确认 Dispatcher 活动工作为 0，再重启 `com.xrayai.axiom` 与 `com.xrayai.axiom.runner` 并恢复任务。恢复后的 `rr000096` 正常提交 4 个 Explore Intent；新 Lease 的 Pi Session 已确认包含可选 Evidence、未完成写入 Leads 和“不要补跑”的新 Prompt。`i035-i037` 均一次完成且执行/基础设施重试为 0：`i035` 生成 2 个核心 Artifact、3 个 Evidence 和 5 个待验证漏洞，其中一个漏洞只有 Findings 关系、Evidence 为空；`i036` 只生成 findings/leads 两个核心 Artifact，也正常完成；`i037` 在 600 秒 Execute 的动态 70% 时点收到 steer，生成 2 个核心 Artifact、8 个 Evidence 和 `f035`。这些真实路径验证了可选 Evidence、无 Evidence 漏洞和常规 RPC 收口；缺少 FINISH/收口超时的核心文件兜底由自动化模拟测试覆盖。
