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
| Reason 创建 pending Intent | 已验证 | Workspace commit，不指定 Worker |
| 空闲 Worker 自动领取 pending Intent | 已验证 | Reconciler + Claim API + E2E |
| Claim lease 与 heartbeat | 已验证 | claim 后立即心跳，初始化阶段也覆盖 |
| API-only Runner | 已验证 | Runner 不访问数据库 |
| API 暂时不可用自动恢复 | 已验证 | 断开/恢复演练，Runner PID 保持不变 |
| stale lease 恢复 | 已验证 | `recover-stale` 和回归测试 |
| execution / infrastructure 重试分离 | 已验证 | 两套计数和退避状态 |
| 停止任务取消本地 Worker | 已验证 | Runner 在阶段边界检查 Task 状态 |
| Goal 是唯一完成指标 | 已验证 | Goal Evaluation 服务和任务收口 |
| Reason 同批 Todo 临时引用 | 已验证 | `todo:<draft-ref>` 在事务内解析为真实 Todo ID；未知引用整批回滚 |

## C. Bootstrap / Reason / Explore

| 能力 | 状态 | 当前证据 |
|---|---|---|
| Bootstrap 广度优先产物 | 已验证 | 三字段 JSON、Artifact 原子注册 |
| 阶段提示词中文约束 | 已验证 | bootstrap/reason/explore/conclude prompts |
| Reason 单层 Todo | 已验证 | category/theme/status/source refs |
| Todo 跨 Reason 轮次更新 | 已验证 | Reason Workspace 事务提交 |
| Reason 动态触发 | 已验证 | batch、low watermark、max wait、retry-exhausted |
| Reason 动态时间预算 | 已验证 | Intent 结构化 execute/conclude seconds |
| Explore 文件优先结果 | 已验证 | `explore_result.json` + Artifact |
| Explore JSON 有界修复/收口 | 已验证 | 同会话修复和共享 deadline 内重试 |
| Fact 是本次运行中文摘要 | 已验证 | Runner 校验后才写 Fact |
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
| 全套测试 | 94 passed，1 个弃用警告 |

最终生产数据目录：`/Users/hexin/.local/share/axiom-v3`。旧目录仍保留，回滚脚本不会删除旧数据。
