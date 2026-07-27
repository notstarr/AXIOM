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

## B. 调度和生命周期

| 能力 | 状态 | 当前证据 |
|---|---|---|
| Bootstrap 第一阶段 | 已验证 | 新任务创建 i001 bootstrap Intent |
| ReasonRun 与 Intent 解耦 | 已验证 | `reason_runs` / `reason_run_sources`；新图谱没有 Reason Intent |
| Reason 创建 pending Intent | 已验证 | ReasonRun Workspace commit 后一次性创建，不指定 Worker |
| 空闲 Worker 自动领取 pending Intent | 已验证 | Reconciler + Claim API + E2E |
| Claim lease 与 heartbeat | 已验证 | claim 后立即心跳，初始化阶段也覆盖 |
| ReasonRun 独立生命周期 | 已验证 | claim/start/input/heartbeat/activity/conclude/release/stale recovery；终态输入指纹去重 |
| 同 Task 单活动 ReasonRun | 已验证 | 数据库唯一约束 + Service 双重校验 |
| Reason Workspace 终态清理 | 已验证 | stop/Goal/release/stale 同事务 abandon；当前 Goal commit Workspace 保持 committed |
| API-only Runner | 已验证 | Runner 不访问数据库 |
| API 暂时不可用自动恢复 | 已验证 | 断开/恢复演练，Runner PID 保持不变 |
| stale lease 恢复 | 已验证 | `recover-stale` 和回归测试 |
| execution / infrastructure 重试分离 | 已验证 | 两套计数和退避状态 |
| 停止任务取消本地 Worker | 已验证 | Runner 在阶段边界检查 Task 状态 |
| Goal 是唯一完成指标 | 已验证 | Goal Evaluation 服务和任务收口 |
| Goal 证据直连 | 已验证 | 新任务 `Fact(s) → Goal`；ReasonRun 不生成图谱节点 |
| Reason 同批 Todo 临时引用 | 已验证 | `todo:<draft-ref>` 在事务内解析为真实 Todo ID；未知引用整批回滚 |
| 多 Task 公平领取 | 已验证 | 按历史最近领取时间轮询；Task 内 Bootstrap → Reason → Explore |
| 全局/Task/Explore 并发双重校验 | 已验证 | Reconciler 纯计算 + 数据库 claim 事务 |

## C. Bootstrap / Reason / Explore

| 能力 | 状态 | 当前证据 |
|---|---|---|
| Bootstrap 广度优先产物 | 已验证 | 三个固定 Markdown、Artifact/Fact 由代码生成 |
| 阶段提示词中文约束 | 已验证 | bootstrap/reason/explore/conclude prompts |
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
| 有界修复/收口 | 已验证 | 同一会话最多 3 次、共享 deadline，只补缺失产物/操作，不重做网络探索 |
| Fact 由代码生成 | 已验证 | Bootstrap 引用三个 Artifact；Explore 引用原 Intent、Artifact 和漏洞 ID |
| 通过 Context ref 按需展开 | 已验证 | Manifest/index/detail/expand API |
| ASM 替代 Bootstrap | 后续方案 | 当前明确不进入主流程 |

## D. Context、Artifact、文件

| 能力 | 状态 | 当前证据 |
|---|---|---|
| Context Manifest | 已验证 | `contexts/{task}/r.../manifest.json` |
| 摘要/索引优先 | 已验证 | preview、detail ref、按需 expand |
| Artifact 元数据索引 | 已验证 | hash、size、storage_key |
| 现有文件管理复用 | 已验证 | FileStore 和前端 Files 页面 |
| 任务文件生命周期 | 已验证 | 外部 data-dir、路径安全、归档/删除 |
| Worker 文件安全收集 | 已验证 | 拒绝 symlink/越界/空文件；不扫描 session、process、models.json |
| Artifact 单份正式内容 | 已验证 | 成功 adopt 后清理 run 副本；内部索引不注册为 Artifact |
| Project Knowledge | 已验证 | 项目共享知识索引 |

## E. 漏洞、上报和外围能力

| 能力 | 状态 | 当前证据 |
|---|---|---|
| 通用 Vulnerability 对象 | 已验证 | 内部与外部报告共用字段 |
| 漏洞搜索、状态、严重性、归档 | 已验证 | 漏洞管理页面和 API |
| 外部上报与失败重试记录 | 已验证 | external report attempts |
| Skill 共享路径注入 | 已验证 | Pi 通过 `--skill` 读取共享目录，不复制内容 |
| Skill 按需查阅提示 | 已验证 | 三阶段 prompt 明确先查目录再按需读取 |
| MCP 多格式导入 | 已验证 | JSON/YAML/TOML、Server 和工具发现 |
| MCP 工具启停 | 已验证 | Server/单工具状态和前端展示 |
| Worker 自定义 MCP | 已验证 | Worker 配置独立于左侧 MCP 管理 |
| Guard 审计和决策 | 已验证 | Guard 模块、扩展和审计事件 |
| Guard 配置真实持久化 | 已验证 | off/audit/enforce、白名单、硬规则、提示词均进入 RuntimeConfig 并实际决策 |
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
| 图谱 Todo 侧栏 | 已验证 | 任务图谱页面 |
| Files / MCP / Worker / Settings | 已验证 | 生产页面验收 |
| RuntimeConfig 四组设置 | 已验证 | 调度并发、阶段预算、Reason 触发与派发、高级可靠性 |
| 设置版本与热应用状态 | 已验证 | `expected_version` / 409；已保存版本与 Runner applied 真实显示 |
| 合法零值刷新不回退 | 已验证 | 浏览器将 low watermark 保存为 0、刷新持久化，再恢复默认 1 |
| ReasonRun 活动卡片 | 已验证 | 显示 rr ID、Worker、活动、心跳、触发器、预算和配置快照；图谱无节点 |
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
| Alembic Revision | `20260727_0020`；空库及包含真实 ReasonRun 数据的生产副本升级-回滚-再升级通过；回滚物化旧版 Reason Intent 保留关联审计 |
| ReasonRun 生产备份 | `/Users/hexin/.local/share/axiom-v3/backups/20260727-131804-reasonrun-final/` |
| Reason CLI PATH | 已验证：Pi 子进程注入当前虚拟环境 `bin`，生产 `t_138` 直接调用 `axiom reason ...` |
| ReasonRun/FINISH E2E | `t_138` 完成；2 Intent、2 ReasonRun、4 Artifact、1/1 Todo、零重试、`Fact f002 → Goal`、无活动工作 |
| 停止即时取消 | 运行中 ReasonRun 转为 cancelled；Workspace abandoned；旧 heartbeat/commit 409；临时资源已清理 |
| RuntimeConfig 热应用 | 浏览器保存/刷新/合法 0/Runner ACK 均通过；最终 saved/applied 均为 v210 |
| 最终生产 smoke | 临时 `t_141` 验证 Bootstrap → ReasonRun → stop；图谱无 Reason Intent，活动工作和 open Workspace 均为 0 |
| 全套测试 | 163 passed，1 个弃用警告 |

最终生产数据目录：`/Users/hexin/.local/share/axiom-v3`。旧目录仍保留，回滚脚本不会删除旧数据。
