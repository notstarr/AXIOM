# AXIOM 重构迁移清单（最终核对）

状态含义：

- **已验证**：当前代码、测试和运行证据一致。
- **保留兼容**：保留数据或后端能力，但不是新任务主流程。
- **后续方案**：已有设计方向，但不属于本次生产切换验收。

## A. 数据和任务边界

| 能力 | 状态 | 当前证据 |
|---|---|---|
| 模块化 AXIOM 后端 | 已验证 | `/Users/hexin/vscode/xrayAI/axiom/src/axiom/modules` |
| Project / Task CRUD | 已验证 | API、前端任务管理、测试 |
| Origin 结构化解析 | 已验证 | 域名/IP、scheme、显式 port 和原始输入 |
| Task 级 Scope 黑白名单 | 已验证 | 黑名单优先；Reason 派发时后端校验 |
| Task Hints | 已验证 | 创建事务写入，阶段 Context 可读取 |
| 项目凭证 | 已验证 | 凭证模块和迁移数据 12 条 |
| 项目级旧黑白名单 | 保留兼容 | 不再作为新 Scope 配置入口 |
| 旧任务只读迁移 | 已验证 | `legacy_readonly`，禁止修改执行图谱 |
| Fact/Intent 链路级联归档 | 已验证 | `archive_batches/archive_batch_members`、下游闭包、重叠批次恢复 |
| 历史归档兼容迁移 | 已验证 | 0025 为 Fact/Intent/Artifact/Vulnerability/Todo 生成可读批次 |

## B. 调度和生命周期

| 能力 | 状态 | 当前证据 |
|---|---|---|
| Bootstrap 第一阶段 | 已验证 | 新任务创建 i001 bootstrap Intent |
| ReasonRun 与 Intent 解耦 | 已验证 | `reason_runs` / `reason_run_sources`；新图谱没有 Reason Intent |
| Reason 创建 pending Intent | 已验证 | ReasonRun Workspace commit 后一次性创建，不指定 Worker |
| 空闲 Worker 自动领取 pending Intent | 已验证 | Reconciler + Claim API + E2E |
| Claim lease 与 heartbeat | 已验证 | claim 后立即心跳，初始化阶段也覆盖 |
| Pi 运行时非启动式预检 | 已验证 | 固定路径 + package metadata + Node 版本；不再用完整 `pi --version` 阻塞 Bootstrap claim |
| ReasonRun 独立生命周期 | 已验证 | claim/start/input/heartbeat/activity/conclude/release/stale recovery；终态输入指纹去重 |
| ReviewRun 独立生命周期 | 已验证 | 待验证漏洞原子排队；claim/start/input/heartbeat/activity/decision/conclude/complete/release/stale recovery |
| 同 Task 单活动 ReasonRun | 已验证 | 数据库唯一约束 + Service 双重校验 |
| 同漏洞单活动 ReviewRun | 已验证 | 部分唯一索引 + Service 双重校验；内容更新取消旧 Lease |
| Reason Workspace 终态清理 | 已验证 | stop/Goal/release/stale 同事务 abandon；当前 Goal commit Workspace 保持 committed |
| API-only Runner | 已验证 | Runner 不访问数据库 |
| API 暂时不可用自动恢复 | 已验证 | 断开/恢复演练，Runner PID 保持不变 |
| stale lease 恢复 | 已验证 | `recover-stale` 和回归测试 |
| execution / infrastructure 重试分离 | 已验证 | 两套计数和退避状态 |
| 停止任务取消本地 Worker | 已验证 | Runner 在阶段边界检查 Task 状态 |
| Goal 是唯一完成指标 | 已验证 | Goal Evaluation 服务和任务收口 |
| Goal 证据直连 | 已验证 | 新任务 `Fact(s) → Goal`；ReasonRun 不生成图谱节点 |
| Reason 同批 Todo 临时引用 | 已验证 | `todo:<draft-ref>` 在事务内解析为真实 Todo ID；未知引用整批回滚 |
| 多 Task 公平领取 | 已验证 | 主链 Task 优先、同层按历史最近领取轮询；Task 内 Bootstrap → Reason → Explore → Reviewer，审核只使用剩余容量 |
| 全局/Task/Explore/Reviewer 并发双重校验 | 已验证 | Reconciler 纯计算 + 数据库 claim 事务；Reviewer 单 Task 并发固定 1 |
| 四个逻辑 Worker 与模型后端池 | 已验证 | `/api/v1/worker-phases`；同一后端可加入多个阶段池，Reviewer 初始复制 Reason 后端 |
| 通用/阶段专属能力矩阵 | 已验证 | `phase_capability_bindings`；Lease `phase-capabilities.json` 快照 |

## C. Bootstrap / Reason / Explore / Reviewer

| 能力 | 状态 | 当前证据 |
|---|---|---|
| Bootstrap 广度优先产物 | 已验证 | 三个固定 Markdown、Artifact/Fact 由代码生成 |
| 阶段提示词中文约束 | 已验证 | bootstrap/reason/explore/review 及各自 conclude prompts |
| Reason 单层 Todo | 已验证 | category/theme/status/source refs |
| Todo 跨 Reason 轮次更新 | 已验证 | Reason Workspace 事务提交 |
| Reason 动态触发 | 已验证 | 初始、batch、low watermark、max wait、失败终态、可执行 Todo、指纹去重 |
| Reason 动态时间预算 | 已验证 | Intent 结构化 execute/conclude seconds |
| Reason 派发 Intent 限额 | 已验证 | 目标开放数 + 单轮新增上限；Todo 不受影响；commit 二次校验 |
| 阶段统一 FINISH | 已验证 | 只接受最后一条 assistant 纯文本 `FINISH`；随后再校验产物/Workspace |
| Bootstrap 固定文件产物 | 已验证 | 三个非空 Markdown；Artifact 与 Fact 由代码生成 |
| Reason 增量控制面 | 已验证 | Axiom CLI 经 HTTP 逐条写 Workspace，已移除 `/submit` 和模型大型 JSON |
| Reason 无漏洞上报 | 已验证 | Workspace 无 vulnerability 字段；只有 Explore 可自动上报 |
| Explore 专用 Artifact 目录 | 已验证 | 只收集 `{run_dir}/artifacts/`，内部文件不会误注册 |
| Explore 漏洞增量上报 | 已验证 | `axiom explore vulnerability report` 逐条校验并按 lease 保存 |
| 待验证漏洞自动排队审核 | 已验证 | Explore 完成/人工创建与 Finding/Evidence 同事务创建 ReviewRun；历史待验证由 0034 补队列 |
| Reviewer 结构化裁决 | 已验证 | verified/duplicate/ignored/needs-human；原因必填，duplicate 只允许更早同 Task 漏洞 |
| Reviewer 独立产物 | 已验证 | `review.md` 必填、`evidence/**` 可选；注册 `review.report/review.evidence` 并以 `review_run_id` 归属 |
| Reviewer 不改漏洞正文 | 已验证 | 只能更新状态、状态原因和重复引用；不能创建 Fact/Todo/Intent 或触发 Reason |
| Reviewer 后处理 Task | 已验证 | Task running/stopped/completed/failed 均可审核；暂停不取消，归档漏洞会取消 |
| 有界修复/收口 | 已验证 | 同一会话最多 3 次、共享 deadline，只补缺失产物/操作，不重做网络探索 |
| Fact 由代码生成 | 已验证 | Bootstrap 引用三个 Artifact；Explore 引用原 Intent、Artifact 和漏洞 ID |
| 通过 Context ref 按需展开 | 已验证 | Manifest/index/detail/expand API |
| ASM 替代 Bootstrap | 后续方案 | 当前明确不进入主流程 |

## D. Context、Artifact、文件

| 能力 | 状态 | 当前证据 |
|---|---|---|
| Context Manifest | 已验证 | `contexts/{task}/r.../manifest.json` |
| 摘要/索引优先 | 已验证 | Fact/Intent/Artifact 等使用 preview；全文高价值列表去除重复 preview，保留 detail ref 与按需 expand |
| Artifact 元数据索引 | 已验证 | hash、size、storage_key |
| 现有文件管理复用 | 已验证 | FileStore 和前端 Files 页面 |
| 任务文件生命周期 | 已验证 | 外部 data-dir、路径安全、归档/删除 |
| Worker 文件安全收集 | 已验证 | 拒绝 symlink/越界/空文件；不扫描 session、process、models.json |
| Artifact 单份正式内容 | 已验证 | 成功 adopt 后清理 run 副本；内部索引不注册为 Artifact |
| Reviewer 目标 Context | 已验证 | `review_target.json` 提供唯一目标和 Finding/Evidence `content_path`，普通索引继续可用 |
| Reviewer 文件夹 | 已验证 | 文件中心按 `review_run_id` 精确分组和深链，不混入 Task 文件或 Intent 文件夹 |
| Project Knowledge | 已验证 | 项目共享知识索引 |
| 归档 Context 隔离 | 已验证 | 新 revision/expand 排除归档实体、关系和 Goal evidence；旧 revision 不改写 |
| 归档 Artifact 保留 | 已验证 | 文件不删除，独立归档接口支持预览和下载 |

## E. 漏洞、上报和外围能力

| 能力 | 状态 | 当前证据 |
|---|---|---|
| 通用 Vulnerability 对象 | 已验证 | 内部与外部报告共用字段 |
| 漏洞搜索、状态、严重性、归档 | 已验证 | 漏洞管理页面和 API |
| 漏洞 Reviewer 历史 | 已验证 | 等待/运行/完成/失败、Worker、审核原因、重复蓝链、Artifact、人工重审 |
| 外部上报与失败重试记录 | 已验证 | external report attempts |
| 外部正式上报门禁 | 已验证 | 仅 `已验证` 漏洞可 submit/retry；预览仍开放 |
| Skill 共享路径注入 | 已验证 | Pi 通过 `--skill` 读取共享目录，不复制内容 |
| Skill 按需查阅提示 | 已验证 | 四阶段 prompt 明确先查目录再按需读取；`vuln-review` 默认仅 Reviewer |
| MCP 多格式导入 | 已验证 | JSON/YAML/TOML、Server 和工具发现 |
| MCP 工具启停 | 已验证 | Server/单工具状态和前端展示 |
| MCP 按具体 Tool 的阶段分配 | 已验证 | `common/bootstrap/reason/explore/review`；只注册选中的 Tool |
| 旧 Worker MCP 绑定 | 保留兼容 | 只读投影；新写入统一走阶段能力矩阵 |
| 内置 Tool 固定目录 | 已验证 | Pi 原生 + AXIOM 阶段 CLI；API/UI/Lease 共用代码注册表且只读 |
| 扩展 Tool 按阶段 Extension 加载 | 已验证 | `--extension` + `axiom-tool-activation`；JSON/RPC 共用快照 |
| 五个临时样例退休 | 已验证 | 0032 清理目录与绑定，保留 Audit；新 Lease 不再出现 |
| Skill 按阶段过滤 | 已验证 | Lease 只注入通用能力与当前阶段专属目录 |
| Bootstrap Conclude 无 Bash | 已验证 | 原生工具目录、阶段 Prompt 与追加 System Prompt 一致 |
| Guard 审计和决策 | 已验证 | Guard 模块、扩展和审计事件 |
| Guard 配置真实持久化 | 已验证 | off/audit/enforce、白名单、硬规则、提示词均进入 RuntimeConfig 并实际决策 |
| Explore 原生浏览器 Tool | 已验证 | `browser-control` Skill 启用时加载 19 个 `browser_*` Tool；Bootstrap/Reason/Reviewer 不加载 |
| Browser Broker Lease 隔离 | 已验证 | Runner 常驻单 CDP 连接；Lease 私有 page/ref；Execute→Conclude 复用；Lease 结束统一清理 |
| 浏览器流量增量审计 | 已验证 | HTTP/WebSocket/Console/Dialog/下载写入 `process/browser/`；游标读取不清空共享数据 |
| 旧 URL 守门页面 | 保留兼容 | 新前端不展示，新路由不作为能力入口 |
| 旧爬虫页面 | 保留兼容 | 后端历史逻辑保留，前端隐藏 |

## F. 前端

| 能力 | 状态 | 当前证据 |
|---|---|---|
| 保留旧版信息架构和视觉习惯 | 已验证 | V2 适配层接入现有静态前端 |
| 新建任务自由翻页 | 已验证 | 两页可任意切换 |
| 默认目标、Worker 全选 | 已验证 | 新建任务表单 |
| 任务 Scope/Hints/Worker 配置 | 已验证 | 任务详情和新建任务 |
| 概览/图谱切换 | 已验证 | 生产浏览器验收 |
| 独立归档视图 | 已验证 | 批次列表/详情、影响预览、Artifact 查看、重叠恢复计数和恢复历史 |
| 图谱 Todo 侧栏 | 已验证 | 任务图谱页面 |
| Files / MCP / Worker / Settings | 已验证 | 生产页面验收 |
| Worker 集中能力抽屉 | 已验证 | 四张逻辑 Worker 卡片；内置/Skill/MCP/扩展五个 Tab；草稿一次矩阵保存 |
| 资源页面绑定只读化 | 已验证 | Skill/MCP/工具页只读阶段徽章并深链回 Worker 唯一编辑入口 |
| MCP Server 分组不扩大授权 | 已验证 | Server 级批量操作最终仍保存具体 MCP Tool ID |
| Worker 能力冷启动深链 | 已验证 | 路由先快照目标、数据加载后消费；静态资源版本同步刷新 |
| RuntimeConfig 四组设置 | 已验证 | 调度并发、阶段预算、Reason 触发与派发、高级可靠性 |
| Token 用量持久化 | 已验证 | Pi Session JSONL 明细、四类 Token、完整性状态和幂等回填 |
| Dashboard / Task Token 统计 | 已验证 | 时间范围、实际模型、缓存命中率、任务占比及 Worker/阶段/模型排行 |
| 设置版本与热应用状态 | 已验证 | `expected_version` / 409；已保存版本与 Runner applied 真实显示 |
| 合法零值刷新不回退 | 已验证 | 浏览器将 low watermark 保存为 0、刷新持久化，再恢复默认 1 |
| ReasonRun 活动卡片 | 已验证 | 显示 rr ID、Worker、活动、心跳、触发器、预算和配置快照；图谱无节点 |
| Task 单次运行窗口 | 已验证 | 新建/Resume 支持不限时或整分钟时长；每次 Resume 覆盖旧窗口 |
| 到期自动暂停 | 已验证 | Runner tick 先执行 Deadline Reconciler；原子取消 Intent、ReasonRun、Workspace 和 Lease |
| 截止时间调度守卫 | 已验证 | Claim 与 Reason Trigger 在事务内拒绝已过期 Task |
| 任务概览实时循环 | 已验证 | 最近 2000 条 Audit 首次还原、`after_id` 增量、ReasonRun/Workspace 重试合并、历史循环详情 |
| 概览阶段状态动画 | 已验证 | Bootstrap/Reason/Explore 运行、等待、完成、失败状态及 `prefers-reduced-motion` |
| 当前/最近 Intent 选择 | 已验证 | 活动状态优先；无活动时展示最近非空 ReasonRun 批次，不回退到最早 Intent |
| Fact / Leads / Artifact 来源深链 | 已验证 | 显示生产 Intent，统一进入文件中心并定位目标 Artifact；缺失/归档对象明确提示 |
| Reviewer 前端行为 | 已验证 | 审核历史加载、失败/终态重审、活动禁用、重复漏洞蓝链、Review Artifact 精确深链、外发门禁 |
| 概览聚合计数 | 已验证 | 使用 Task 的 artifact/vulnerability 总数，Fact 排除 `goal/origin`，Leads 独立读取 |
| 运行时间前端 | 已验证 | 任务列表、概览、图谱共用倒计时；Resume 使用共享弹窗 |
| Dashboard 加载态 | 已验证 | 未完成加载不再误显示 0 |
| Tailwind CDN 警告 | 已知非阻塞 | 后续可替换为构建 CSS |

## G. 生产切换证据

| 项目 | 结果 |
|---|---|
| 旧数据库一致性快照 | 已完成 |
| 迁移警告 | 0 |
| foreign key check | 空 |
| 新 API `/health` | `api: v1` |
| 旧 API 回滚 `/health` | `api: v2` |
| 新 API/Runner LaunchAgent | 均运行 |
| 回滚后恢复新运行时 | 已完成 |
| Alembic Revision | 生产已升级为 `20260804_0034`；Reviewer 已投入运行 |
| Explore intervention queue | 待验证 | `intent_interventions` 绑定当前 Explore Lease；升级后验证 API/Runner 读取正常，未发送消息在 Lease 结束时变为 `expired` |
| Reviewer 0034 迁移演练 | 已完成 | 0033 非空库升级/降级；历史待验证补队列、Reason 后端复制、Skill scope、counter、integrity/FK 均通过 |
| Reviewer 0034 生产备份 | `/Users/hexin/.local/share/axiom-v3/backups/reviewer-0034-20260804-183102/axiom.db.pre-0034.bak`；`integrity_check=ok`、FK 0 |
| Reviewer 0034 生产 smoke | `/health` 正常；4 个逻辑 Worker、Reviewer 7 个后端；迁移补建 60 个历史 ReviewRun。`t_148/vr000001` 完成 `ignored` 裁决，生成 `review.report a408` 与 review Token 记录；Reviewer 与主链并行领取 |
| 三阶段迁移备份 | `/Users/hexin/.local/share/axiom-v3/backups/axiom-20260803-before-0031-103939.db`；`integrity_check=ok` |
| 真实工具目录迁移备份 | `/Users/hexin/.local/share/axiom-v3/backups/axiom-20260804-before-0032-20260804-112743.db`；升级后 `integrity_check=ok`、FK 0 |
| 三阶段 API smoke | `/worker-phases`、`/phase-capabilities`、`/workers/overview` 均 200；7 个后端、14 个 common Skill |
| 真实工具目录 smoke | 15 个内置 Tool、3 个逻辑 Worker、0 个扩展 Tool；五个样例 key 为 0；Worker/工具页明暗主题浏览器验收通过 |
| 新 Lease 能力快照 | `t_154/rr000192`、`t_155/rr000193`、`t_155/i013/i014` 均生成 `phase-capabilities.json` |
| 原生 Browser Broker 本机 smoke | 已完成 | 共享 Chrome-SecTest 上完成 open→snapshot→fill→click→changes→read→console/network→navigate→stale-ref→close；两 Lease 隔离，仅一次 CDP Attach |
| ReasonRun 生产备份 | `/Users/hexin/.local/share/axiom-v3/backups/20260727-131804-reasonrun-final/` |
| 归档批次生产备份 | `/Users/hexin/.local/share/axiom-v3/backups/archive-batches-20260729T034510Z/axiom.db.pre-0025.bak` |
| Reason CLI PATH | 已验证：Pi 子进程注入当前虚拟环境 `bin`，生产 `t_138` 直接调用 `axiom reason ...` |
| ReasonRun/FINISH E2E | `t_138` 完成；2 Intent、2 ReasonRun、4 Artifact、1/1 Todo、零重试、`Fact f002 → Goal`、无活动工作 |
| 停止即时取消 | 运行中 ReasonRun 转为 cancelled；Workspace abandoned；旧 heartbeat/commit 409；临时资源已清理 |
| RuntimeConfig 热应用 | 浏览器保存/刷新/合法 0/Runner ACK 均通过；最终 saved/applied 均为 v210 |
| 最终生产 smoke | 临时 `t_141` 验证 Bootstrap → ReasonRun → stop；图谱无 Reason Intent，活动工作和 open Workspace 均为 0 |
| 归档生产 smoke | 临时 `t_152` 验证下游闭包、Context/Graph/Artifact 隔离、归档 Artifact 读取和 6 项恢复；临时数据已清理 |
| 归档前端验收 | `t_150/f100` 只读预览显示 2 Fact、2 Intent、2 Todo、54 Artifact；空 blockers 时确认按钮可用 |
| 全套测试 | `470 passed, 1 warning`；Ruff、compileall、全部前端 JavaScript、Pi Extension 加载、Shell 语法和 diff check 均通过 |

最终生产数据目录：`/Users/hexin/.local/share/axiom-v3`。旧目录仍保留，回滚脚本不会删除旧数据。
