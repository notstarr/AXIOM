---
tags: [axiom, worker, capability, frontend, mcp, skill]
status: implemented
updated: 2026-08-04
---

# AXIOM Agent 中心式 Worker 能力管理

## 结论

能力分配现在只有一个编辑入口：Worker 页面中的 Bootstrap、Reason、Explore 三个逻辑 Worker。

```text
Worker 卡片
→ 右侧能力抽屉
→ 内置 Tool / Skill / MCP Tool / 扩展 Tool
→ 草稿确认
→ 一次 PUT /api/v1/phase-capabilities
```

Skill、MCP、工具页面只维护资源内容、连接、启停、发现、版本和状态，并以只读阶段徽章展示当前
可见性；它们不再保存第二套阶段绑定。

## Worker 卡片与能力抽屉

能力管理页固定展示三个 Worker 卡片。每张卡片同时汇总：

- 模型后端数量、运行量、容量和主要模型；
- 内置 Tool、Skill、MCP Tool、扩展 Tool 的有效数量；
- MCP Server 数量和能力名称摘要；
- 已绑定但停用、缺失或异常的能力数量。

点击卡片打开右侧抽屉。抽屉包含：

```text
概览 | 内置 Tool | Skill | MCP Tool | 扩展 Tool
```

内置 Tool 读取代码注册表，只读展示 Pi 原生 Tool 与 AXIOM 阶段操作。其余三类能力都直接显示
Bootstrap、Reason、Explore 三列；当前打开的 Worker 列仅作视觉强调，不限制修改其它阶段。

## UI 与底层 scope 的转换

前端不再要求用户理解或手工操作 `common`：

- 三阶段全选时，保存为一个 `common` 绑定；
- 从三阶段全选取消一个阶段时，自动转换为剩余两个阶段的显式绑定；
- “全部阶段”是三阶段选择的快捷操作；
- 锁定 Skill 显示三阶段全选且不可修改；
- 不支持的扩展 Tool 阶段不可新增；
- 停用、缺失或异常能力可以解除旧绑定，但不能新增绑定。

数据库和运行时仍使用：

```text
common / bootstrap / reason / explore
```

因此本次只是更直接的交互层，不改变 `phase_capability_bindings`、Lease 快照或运行时并集语义。

## 草稿与批量操作

抽屉打开时建立前端基线和草稿。单项、全部阶段、当前筛选结果和 MCP Server 级操作都只修改草稿。
底部固定显示变更数量；“保存能力”只发送一次矩阵更新，“取消修改”恢复打开抽屉时的状态。存在
未保存修改时关闭抽屉会要求确认。

支持：

- 名称、描述、Server 搜索；
- 仅看已分配、未分配或异常；
- 对当前筛选结果批量分配或清空当前 Worker；
- MCP Tool 按 Server 折叠；
- Server 级三态批量选择，但最终仍保存具体 MCP Tool ID。

选择某个 MCP Tool 仍只向新 Lease 披露该 Tool，不会因为 UI 按 Server 分组而授权同 Server 的其它
Tool。

## 资源页面与深链

Skill、MCP、扩展 Tool 页面只读显示阶段徽章，并提供“前往 Worker 能力管理”。深链格式为：

```text
#/workers?mode=manage&phase=reason&tab=mcp&capability=<id>
```

Worker 页面冷启动、普通页面内跳转和浏览器返回都会复用同一套路由解析。能力数据加载完成后再消费
定位目标，自动打开正确阶段和 Tab；MCP Tool 会展开所属 Server，并滚动到对应能力。普通
`#/workers` 会清除旧抽屉状态。

本次同步更新 Worker 相关静态 JS/CSS 的版本参数，避免浏览器复用旧 `core.js` 导致冷启动深链存在
参数但抽屉没有打开。

## 不变项

- 不新增数据库表、迁移或 HTTP API；
- 后端配置仍独立管理模型、Base URL、Key、上下文、思考模式、并发、优先级和阶段池；
- 内置 Tool 不进入绑定矩阵；
- 已运行 Lease 不热更新，新 Lease 才读取保存后的能力；
- Guard、Skill、MCP、扩展 Tool 和调度协议不变。

## 验证

自动化覆盖 `common` 转显式阶段、重新合并为 `common`、停用能力解除绑定、阶段不兼容、MCP Server
批量选择、单次矩阵保存以及 Worker 冷启动深链。真实浏览器已检查桌面浅色、桌面暗色和 430px
窄屏抽屉；能力行、固定 Footer、搜索区和阶段按钮均可用，控制台没有新增 warning/error。

最终验证：全量 `426 passed, 1 warning`；Ruff、compileall、全部前端 JavaScript、Tailwind 构建和
`git diff --check` 通过。唯一 warning 是既有 Starlette/httpx 弃用提示。
