# ARTEX → AXIOM 借鉴清单

来源：[RuoJi6/ARTEX](https://github.com/RuoJi6/ARTEX)（Go + Next.js，AGPL-3.0）。
AXIOM 同为 AGPL-3.0，代码可互搬。
源码已全量读过：`guard/`、`intercept/`、`llmpool/`、`traffic/`、`db/`、`agent/`、
`enrich/`、`evidence/`、`notify/`、`llmrec/`、`sidequestion/`、`report/`、
`skills/`、`web/src/app/` 全部 28 个页面。

状态图例：**已批准** / 待决 / 已否决 / MCP 替代 / **已实施**

> 实施记录见本文件末尾「实施进展」一节。A-1 / A-2 / A-3 已于 2026-10-26 落地，
> 并在实施过程中修复了一个独立的 P0 缺陷（Reason/Plan 队首死锁，见 A-4）。

---

## 一、已批准

### A-1　LLM 熔断 + 故障转移　【后端 + 前端】

**对应 ARTEX**：`llmpool/pool.go`（有序 profile 链故障转移）、`llmpool/health.go`
（进程级熔断注册表、退避阶梯 1min→5min→30min、`OpenUntil` 到期自动关闭、
状态持久化到 DB 重启不丢、按上下文窗口跳过装不下的候选）、
`web/src/app/(main)/system/llm/page.tsx`（`正常/已熔断/已关闭` 三态徽章 + `立即恢复` 按钮）。

**AXIOM 现状**：`modules/llm_gateway/api.py` 仅 132 行，纯透传，无重试、无链、无熔断。
唯一相关的 `workers/repository.py:57 set_cooldown` 是**手工填的时间戳**，
由 `dispatch/service.py:516` 在 claim 时跳过——没有失败计数、没有自动触发、没有阶梯。

**为什么是第一优先**：这是唯一一个「不补就会再次停摆、且停摆后无人知晓」的缺口。
`dispatch/service.py:532` 在 `infrastructure_retry_count >= limit` 时把条目永久停车，
等人工恢复。t_234 值守期间因此停摆 **2 次**：provider 408 打爆
`axiom.intents`（i071/i078/i080，停 18 分钟）、`axiom.reason_runs`
（rr004485，停 4 分钟），两次都是靠 SSH 手工重置恢复的。

**验收**：provider 连续失败 N 次后该 profile 自动进入冷却并在 LLM 页显示「已熔断」；
冷却到期自动恢复；另有一个 profile 可用时任务不中断；两个都不可用时长任务不会静默停车。

**备注**：与第 7 条「冷却式熔断自动恢复（P2）」是同一件事，合并为一条，不要重复做。

---

### A-2　脚本正文快照送入裁判　【后端】

**对应 ARTEX**：`intercept/scripts.go`（492 行）+ `intercept/review_context.go`。

用 `mvdan.cc/sh` 解析 shell AST，追踪 `bash -c` 递归、`source`、`python -m` 模块定位、
`cd` 对后续相对路径的影响、变量常量传播、循环绑定、`sed` 写入目标、重定向/heredoc，
最后把**实际将要执行的源码**读成只读快照附进裁判输入（上限 8 个文件 / 1 MiB，
每个带 SHA256）。读不到、超限、非 UTF-8、交互式远端会话 → 标记 `status=incomplete`。

**AXIOM 现状**：`guard/policy.py:233 _judge_input()` 只把 `bash  <command>` 一行
文本交给裁判。任何"破坏性动作藏在被调用文件里"的写法天然不可见。

**为什么重要**：t_234 值守期间我的正则扫描器连续 3 次漏检
（`requests.put` → `urllib.request.Request(method=...)` → 自定义 `req('PUT', ...)`）。
当时以为是正则写漏了，实际是**接口层面只递了命令行文本**——这不是正则能补的洞。

**实现建议**：分两步。先做简化版——正则抽出 `python3 x.py` / `bash x.sh` / `source x`
的路径，读到就附进裁判输入，读不到标 `incomplete`。拿到约八成收益、两成成本，
且与现有 `_judge_input` 接口兼容。真需要处理 `bash -c` 嵌套和变量传播时再上 tree-sitter。

---

### A-3　拦截话术框架　【后端，近零成本】

**对应 ARTEX**：`guard/guard.go:138 systemBlockMessage()`。

把拦截原因包成
`【ARTEX 平台管控·非目标防御】此调用被平台拦截。原因：<reason>。此操作被禁止。`

**AXIOM 现状**：`policy.py` 的 reason 原样回给 worker（如"修改或重置用户密码"）。
这类措辞读起来和目标返回的 WAF / 403 一模一样，而**红队 agent 的本能是绕过 WAF**——
改写命令、换 payload、重新编码、重试。既徒劳（平台拦的是动作类别，不是一个字符串），
方向也错（这是策略裁决，不是待攻克的障碍）。审计行仍存原始 reason，只有回给模型的
tool_result 带这层框架。

**验收**：worker 收到 BLOCK 后转向别的技术路径，而不是重试同类命令。

---

## 二、MCP 替代（不自己做）

### M-1　资产表 + 覆盖度

原计划抄 ARTEX `db/task_scope.go` + `db/assets.go`（1536 行）+ 资产页（1295 行）+
力导向覆盖图。**决定：用 MCP 解决，不在 AXIOM 内建资产表。**

**连带影响**：ARTEX 的覆盖度算法（`Coverage`：`task_scope` 当分母、锚到 fact 节点的
资产当已测，算出总覆盖率 + 分类覆盖率喂给 agent）依赖本仓资产表。改由 MCP 提供资产
清单后，覆盖度需要重新决定是从 MCP 拉还是不算。**暂挂，等 MCP 方案落地再议。**

**附带发现**：`static/templates/assets.html`（343 行）是**孤儿模板**——`index.html`
里没有 `navigateTo('assets')`，没有 `assets.js`，`modules/` 下也没有 `assets/`。
它展示的字段（HTTP 方法、API 路径、凭证、测试状态）来自某个已删除的版本。
MCP 方案定下来后，这块模板要么接上要么删掉，别继续挂着。

---

## 三、待决（尚未批准）

| 编号 | 项 | 说明 | 工作量 |
|---|---|---|---|
| P-2 | 快照不完整 → 不自动放行 | `IncompleteScriptAction` 默认 `ask`。「没看到破坏迹象」≠「没有副作用」。依赖 A-2 | 小 |
| P-3 | 模型失败策略可配 + 重试 | `policy.py` 现有 4 处 `source="fail_open"`：没配模型 / 脱敏异常 / 请求抛错 / 输出解析不出。enforce 下 provider 一抖，guard 等于不存在，而任务还在跑、日志还在涨。ARTEX 默认转人工 + `RetryCount`（上限 20）+ 间隔重试 | 小 |
| P-4 | 任务列表三件事 | 任务分类（`task_categories` 表 + 拖动改分类）、批量暂停/继续/归档、软删→永久删两段式（含 `等待删除/等待归档/等待还原` 过渡态）。我们现在只有单条 stop，任务一多没法用 | 小 |
| P-5 | 定时计划 | 星期多选 + 时区 + **时间窗口**（`窗口运行中/等待窗口/窗口外`，跨天标"次日"）+ `持续运行`。我们只有任务级单次 deadline，没有周期性重跑 | 中 |
| P-6 | 旁路提问 | `sidequestion/`：对正在跑的会话提问，快照当前上下文（过长则摘要），一次 completion 回答，**不污染主会话**，同一会话限一个（`ErrSideBusy`）。我们从三种父节点都缺。等价物干预是**注入**、会改变行为，不是提问 | 小 |
| P-7 | guard 页从只读改可配 | 现在 `guard-audit.html`（148 行）只有筛选和统计。ARTEX 有规则 CRUD + 裁判兜底面板（`模型失败时(重试耗尽后)` / `审批等待超时` / `超时后默认动作` / `脚本快照不完整时`）+ 拦截范围勾选 | 中 |
| P-8 | LLM 原始报文录制 | `llmrec/` provider 装饰器，捕获每次 Stream 完整请求和累积响应。我们 `llm_token_usage` 只存 token 数 + 指向 Pi session 文件的指针，**而那个文件一被裁剪就没了** | 小 |
| P-9 | 漏洞去重合并 | `db/finding_cases.go`：`finding_case_suggestions` 存"这两条可能重复"的建议，人工 accept/veto，合并带事件审计 + `version` 乐观锁 | 中大 |
| P-10 | 流量录制 | `traffic/traffic.go`（2130 行）内嵌 go-mitmproxy，写文件树 + SQLite 索引分页。**只做"记录 agent 发出的一切 HTTP"，不做通用抓包工具** | 大 |
| P-11 | Skills 脚本化 | 我们 22 个 skill、`SKILL.md` 合计 2672 行、**全仓只有 5 个脚本**。ARTEX 只 3 个 skill，但 `api-recon` 一个就带 6 个能跑的脚本（`runtime_harvest.js` Puppeteer 活会话抓 METHOD/body/WS、`build_perm_tree.py` 权限树还原、`preload.js` hook 掉登录门）+ 500 行 reference。**问题不是技能数量，是没有工具** | 按需 |
| P-12 | Prompt 管理 | 我们的 `prompts.js` 是 localStorage 片段剪贴板，和真跑的 prompt 无关；真 prompt 在 `dispatcher/prompts/*.md` 29 个文件，运行期改不了。ARTEX 是 DB + UI 编辑 + 每次运行读最新值。**至少先把收尾词做成可配** | 中 |
| P-13 | 通知渠道矩阵 | 我们只有钉钉、只有 `result_reported`/`result_reviewed` 两个事件。ARTEX 六渠道 + 每渠道独立过滤（最低级别、task/asset 交集、vulnclass 包含排除）。两个细节值得抄：**事件快照冗余存漏洞字段**（事后漏洞会被改级别，推的应是事发当时结论）、**过滤解析失败退化为"不过滤"**（多推一条远好过静默漏一条高危） | 中 |
| P-14 | 凭据结构化 | `agent/credentials.go`：LLM 把原始记录整理成 `{label, username, password, token, cookie, endpoint, protocol}`，提示词明确"不得猜测或生成任何凭据、不把不同账号的字段混成一组"，带 grounding 检查。我们现在是 `name` + 自由文本 | 小 |

---

## 四、已否决（记录理由，避免二次讨论）

| 项 | 理由 |
|---|---|
| UI 整体重写 | 他是 Next.js + React，我们是 22 个模板 + Alpine 风格 JS。重写收益不抵成本，我们信息密度其实更高 |
| `selfupdate` 热更新 | 他是单二进制 + `start.sh` 守护下换装；我们是 systemd + 多进程。风险大于收益 |
| MITM 代理整套（拦截改写部分） | 和 c2 / webshell 通道重叠。只保留 P-10 的录制部分 |
| 自定义 agent 触发器 | `trigger_run_mode`(serial/parallel) + `trigger_merge_mode`。确定性流水线在长任务里更可控 |
| 拦截规则存 DB、内置规则可删 | 我们把 `hard_rule` 留在代码里、运行期不可关。这是**有意的取舍**，不是欠债 |
| 他的规则匹配方式 | `ruleMatches` 拿正则匹配 `string(input)`，即 JSON 转义后的文本，转义符会破坏匹配。我们匹配规范化后的 command，这点我们更好 |
| cold-digest 上下文压缩 | 我们 `context/` 模块 2601 行（`context_revisions` + 注入 + 保留策略）比它完整，他靠 Pi compaction |

---

## 五、我们更强的（不要被"抄"带偏）

- **`axiom_tools`**：manifest + 包哈希 + `risk_tags` + `scopes` + `call_count` + 按阶段绑定。ARTEX custom tool 只有 key/kind/exec/deferred
- **`exports`**：overview.json / timeline / 漏洞 markdown / zip 打包，比 `report/report.go` 全
- **`context`**：2601 行，自建注入 + 保留策略
- **`graph`**：意图/事实/来源/Goal/Todo/队列一图打尽（961 行），信息密度高于 ARTEX 的探索链路图
- **`phase_capabilities`**：1820 行，按阶段做能力合同。ARTEX 没有这个概念
- **`sandboxes` + `archives`**：沙箱生命周期 + 归档批次，比 ARTEX 的 archive 完整
- **`tools` 页**：843 行 vs 我们 174 行，但我们字段更深

---

## 六、顺带记下的事实

- ARTEX 共 28 个页面，AXIOM 13 个导航页 + 5 个二级页。
- AXIOM 后端体量更大：`reason` 5345 行 / `execution` 3316 / `plan` 3185 / `context` 2601，
  ARTEX 对应模块只有几百行。**"功能少"不成立，差距在可靠性、可配置性、交付物质量。**
- ARTEX 页面规模：`tasks` 3633 行、`chat` 1569、`findings` 1347、`assets` 1295、
  `dashboard` 1211、`skills` 1127、`intercept` 1042、`llm` 990、`schedules` 938。
- AXIOM 模板规模：`graph-view` 961、`human-view` 676、`settings` 592、`task-list` 569、
  `guard-audit` **148**、`action-replay` **107**。
- 完全缺失的页面：`chat`、`schedules`、`traffic`、`llm-records`、`sync`、`logs`、
  `agents`、`findings/case`、`intercept/assets`。


---

## 七、实施进展（2026-10-26）

### 已完成

| 编号 | 内容 | 关键文件 |
|---|---|---|
| **A-4** | Reason/Plan 队首死锁 + 停放状态可见 + 停摆告警 | `dispatcher/queue.py`（新）、`reconciler.py`、`dispatch/service.py`、`v2-adapter.js` |
| **A-3** | 拦截话术框架 | `guard/messages.py`（新）、`guard/schemas.py`、`guard/service.py`、`axiom-guard/index.ts` |
| **A-1.2** | 停放条目自动恢复 | `dispatch/recovery.py`（新）、`recovery.py` 迁移、`runner.py` |
| **A-1.1** | Worker 熔断阶梯 | `dispatch/breaker.py`（新）、四处 release 接线 |
| **A-1.4** | 熔断态徽章 + 立即恢复 | `workers.html`、`worker-tools.js`、`phase_capabilities/schemas.py` |
| **A-2** | 脚本正文快照送入裁判 | `guard/scripts.py`（新）、`guard/prompts.py`（新）、`guard/policy.py`、`axiom-guard/index.ts` |

### A-4　Reason/Plan 队首死锁（新增，非 ARTEX 借鉴）

**这是另一个会话发现的真实停摆，经核验成立，且比其描述多一处。**

三处成因（都已修）：

1. `reconciler.py` 先 `[:1]` 截队首、后过滤 `retry_blocked`，导致被 park 的队首一次性封锁整条队列；
2. `_selected_plan_queue` 同一个错（`pending[:1]`）；
3. claim 侧 `pending_runs_for_task` 返回含 park 行，FIFO 校验用未过滤列表 → 即使调度器给出健康条目也会被拒；
4. Plan claim 的「已有排队中的 ReasonRun」检查同样用未过滤列表 → 一个 park 的 ReasonRun 让 Plan 永远领不到（**另一会话漏掉的第三处**）。

修法：新增 `dispatcher/queue.py`，`ready_runs` / `fifo_head` 单一谓词，调度器与 claim 侧共用，口径不可能再漂移。

另外两处顺带修正：

- **停放状态被前端吃掉**：`v2-adapter.js` 的 `.map()` 丢弃了 `activity_kind/activity_summary`，`queuedReasonDisplay` 又写死成「等待前序 Reason 完成后按 FIFO 领取」。数据库里真实写的是「Reason 基础设施重试已达上限，等待人工或系统恢复」——这条真实状态被覆盖，是「排队中 2 小时」显示的来源。已改为透传真实状态，`retry_blocked` 渲染为「已阻塞 · 等待自动恢复」。
- **claim 报错顺序**：run 耗尽预算这件事与哪个 worker 无关，原先却晚于「Worker is cooling down」抛出，会把操作员引向错误方向。已把 item 级判定提到 worker 级之前。

### A-1 的实际做法（与最初设想的差异）

- **不做单次运行内跨 provider 故障转移**。AXIOM 的 LLM 出流由 Pi 子进程直连 provider，`models.json` 只注册一个 provider；`llm_gateway` 仅在 `backend.llm_proxy=True` 且全局 `network_proxy_mode=="http"` 时启用（`runner.py` → `llm_proxy_for`）。在网关做多 provider 链对多数部署是死代码。需要先「网关始终启用」（顺带成为 P-8 原始报文录制的收流点），单独排期。
- 实际交付的是**跨 worker 故障转移 + 自动恢复**：breaker 把坏后端移出轮转，`narrow_worker_pool` 把重试导向另一个 worker，`recover_parked` 兜住「只有一个后端」的情形。这正是 t_234 两次停摆的机制。
- **没有抬高 `infrastructure_retry_limit`**（另一会话建议 3→5）。自动恢复已承担容错：有效总尝试 = `3 × (1 + 5) = 18` 次。抬 limit 只会拖长单条目的最坏恢复时间。

### A-2 的关键取舍

**由 Worker 侧扩展读文件，服务端绝不去读盘。** `GuardRequest.cwd` 是 worker 所在环境的路径；Cube 沙箱下那是容器内路径，serve 读到同名本地文件就是**读错了文件**，比不读更危险。

**脱敏顺序是个真问题。** 初版对整段文本统一 `redact`，导致两个 bug：
1. 64 位十六进制摘要命中凭据启发式，`sha256=...` 被替换成 `sha256=[令牌]`；
2. `_SHELL_ASSIGN` 的裸值分支 `[^\s;&|]+` 会吃掉行尾，代码行变成 `Key=[密钥]`。

修法：逐个脚本脱敏、脱敏后再算摘要、摘要最后渲染。摘要因此描述**裁判实际看到的文本**，这正是审计需要的语义。

**顺带补了一个脱敏漏洞**：`_CLOUD_KEY` 原先不认 `sk-` 前缀（OpenAI / Anthropic / 本部署自己的 SiliconFlow key），只有 `sk_live_` / `sk_test_`。A-2 让脚本正文也要过裁判，而部署脚本正是这类 key 最可能出现的地方，已补 `sk-(?:[A-Za-z0-9]+-)*[A-Za-z0-9]{16,}`。

**已知限制（记录，未修）**：`_SHELL_ASSIGN` 仍会裁掉代码行尾。它与 shell 脱敏共用，而 shell 里裸值确实可能含这些字符；改动风险大于收益。影响是丢掉一行末尾的键名，不会丢整段快照。

### 迁移

`20261026_pg0028_retry_recovery`：`workers` 加 4 列（熔断），四张生命周期表各加 `retry_blocked_at` + `recovery_round`，外加 4 个部分索引。已验证升级 / 降级往返。

**历史停放行不回填**：升级后的首次部署，既有 park 行因为 `retry_blocked_at` 为空**不会**被自动恢复。需要跑一次 `POST /api/v1/dispatch/recover-parked`（或手工 UPDATE 一次 `retry_blocked_at`）。这是刻意的——凭空编造停放时间会让首次恢复一次性放行一个未知历史积压。

`sqlite_to_postgresql.py` 的 `LEGACY_SQLITE_COLUMN_DEFAULTS` 已同步 6 张表的默认值，导入器测试全绿。
