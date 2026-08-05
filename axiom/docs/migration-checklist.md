# AXIOM 重构迁移清单（最终核对）

状态含义：

- **已验证**：当前代码、测试和运行证据一致。
- **观察中**：功能已上线且当前检查正常，但约定的时间窗口尚未结束。
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
| Task 级可逆归档 | 已验证 | `tasks.archived`；停止调度、隐藏正常产物、保留全部历史，恢复不自动 Resume |

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
| Pi JSON Provider 终态分类 | 已验证 | 退出码 0 仍识别最终 `stopReason=error/auto_retry_end=false`；恢复成功不误报 |
| Reason transient Worker 冷却 | 已验证 | Provider 错误跳过 Conclude，只增加基础设施重试并写入 `cooldown_until` |
| 停止任务取消本地 Worker | 已验证 | Runner 在阶段边界检查 Task 状态 |
| 归档 Task 调度隔离 | 已验证 | Snapshot、Reason Trigger、stale recovery、阶段 start 均拒绝归档 Task |
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
| 阶段 FINISH 合同 | 已验证 | Bootstrap/Reason/Reviewer 严格；Explore Execute 有限归一化，Conclude 有核心文件兜底 |
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
| Task 归档产物隔离 | 已验证 | Hint/Graph/Todo/Goal/Artifact/Vulnerability/Context 均在恢复前拒绝披露 |

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
| Task 归档与恢复入口 | 已验证 | 列表、概览、图谱、归档视图统一入口；归档后只显示锁定说明和恢复操作 |
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

## G. PostgreSQL 原生化与生产切换

| 项目 | 状态 | 当前证据 |
|---|---|---|
| PostgreSQL 18.4 固定镜像 | 已验证 | compose.yaml 使用 Debian 镜像及不可变 digest；生产端口 127.0.0.1:55432 |
| 三角色与独立 Schema | 已验证 | axiom_owner、axiom_migrator、axiom_app；业务对象位于 axiom Schema；回收 public 公共 CREATE |
| PostgreSQL 唯一运行时 | 已验证 | Engine 拒绝非 PostgreSQL URL；API/Runner 无 --db-path；/health 返回 backend=postgresql |
| JSONB / TIMESTAMPTZ / BOOLEAN | 已验证 | PostgreSQL真实类型；JSONText、UTCDateTimeText 仅保持历史服务层字符串合同 |
| PostgreSQL Alembic 基线 | 已验证 | SQLite 历史 Head 冻结为 20260805_0035；生产使用独立 20260805_pg0001 / postgresql@head |
| 空库升降级 | 已验证 | PostgreSQL upgrade → downgrade → upgrade 完整通过 |
| 行锁领取 | 已验证 | Intent、ReasonRun、ReviewRun 使用 FOR UPDATE SKIP LOCKED；领取和 Lease 创建同事务 |
| 事务重试 | 已验证 | 40P01/40001 最多三次带抖动重试；连接池、锁超时和 idle transaction timeout 生效 |
| SQLite 运行时语义清理 | 已验证 | 运行时代码无 BEGIN IMMEDIATE、PRAGMA、SQLite UPSERT、sqlite_where 或文件锁；SQLite 只用于导入器 |
| PostgreSQL pytest | 已验证 | 普通测试使用 127.0.0.1:55433/axiom_test；SQLite 仅用于固定历史导入 fixture |
| SQLite 只读导入器 | 已验证 | 精确版本、integrity/FK、JSON/时间/ID、空目标、单事务、计数/哈希/Counter/Artifact 校验 |
| 第一份生产副本演练 | 已验证 | /Users/hexin/.local/share/axiom-v3/postgresql-migration/rehearsal-pg18-1d-20260805T180356Z |
| 第二份生产副本演练 | 已验证 | /Users/hexin/.local/share/axiom-v3/postgresql-migration/rehearsal-pg18-2b-20260805T181943Z |
| 正式切换 | 已验证 | /Users/hexin/.local/share/axiom-v3/postgresql-migration/cutover-20260805T183453Z |
| 数据一致性 | 已验证 | 36 张业务表计数/规范化 SHA-256 全匹配；Counter 调整 0 |
| Artifact 一致性 | 已验证 | 3,547 / 3,547 实体文件存在；只迁移元数据，不移动文件 |
| API 快照一致性 | 已验证 | SQLite/PostgreSQL 所有 section hash 一致 |
| 首次备份恢复 | 已验证 | custom-format dump 恢复后数据库 fingerprint 与源完全一致；frozen API 可启动并导出快照 |
| 四阶段真实生命周期 | 已验证 | Bootstrap、Reason Workspace、Explore Artifact/Fact/漏洞、Reviewer 裁决共 15 项检查通过；临时数据已清理 |
| 明文配置迁移 | 已验证 | Credential、API Key、Worker/MCP 配置按原值迁移；不增加加密、主密钥、Secret 引用或接口脱敏 |
| 环境文件权限 | 已验证 | /Users/hexin/.local/share/axiom-v3/postgresql.env 为 0600；LaunchAgent 只保存文件路径，不内嵌连接 URL/密码 |
| 每日备份 | 已验证 | pg_dump --format=custom、globals 和 SHA-256 manifest；14 日备份 + 8 周备份 |
| 24 小时生产观察 | 观察中 | com.xrayai.axiom.postgresql-observer 正在运行并持续写样本；当前 deadlock、等待锁和重复活动 Reason/Review 均为 0；等待 summary.json passed=true |
| SQLite 回滚 | 已验证 | PostgreSQL 已开放写入；sqlite_rollback_allowed=false，明确禁止回退，后续只允许 PostgreSQL备份恢复或修复前进 |

## H. 最终质量门禁

~~~text
.venv/bin/pytest -q                         488 passed, 1 warning
.venv/bin/ruff check src tests migrations   passed
.venv/bin/python -m compileall -q src/axiom passed
node --check <all non-vendor static js>     passed
git diff --check                            passed
~~~

唯一 warning 是既有 Starlette/httpx 弃用提示。生产健康检查、PostgreSQL容器、API/Runner、备份 LaunchAgent 和观察 LaunchAgent 均正常。

完整实现与切换说明：docs/2026-08-05-SQLite到PostgreSQL原生化与生产切换.md。日常启动、维护状态、备份、恢复和故障处理见 docs/postgresql-operations.md。
