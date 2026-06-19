# Cairn 原始设计

## 核心理念

**通用状态空间搜索引擎**——渗透测试只是一种已验证的应用场景。

给定一个起点（Origin）和一个目标（Goal），在黑板上逐步构建事实-意图图，搜索从起点到终点的未知路径。没有固定角色、没有预设工作流。

## Blackboard 架构

```
        origin ──→ f001 ──→ f002 ──→ f003 ──→ ... ──→ goal
                     │         │         │
                     i001      i002      i003
                  (explore)  (explore)  (explore)
```

三个原语：

| 概念 | 含义 | 例子 |
|------|------|------|
| **Fact** | 已确认的客观发现，写在黑板上 | "端口 8080 运行 Tomcat 9.0.50" |
| **Intent** | 声明的探索方向，尚未执行 | "对 /api/login 测试 SQL 注入" |
| **Hint** | 人类注入的提示，worker 下次读图时获取 | "试试弱口令 admin/admin" |

## 三种任务类型

同一个 Worker 执行三种任务：

| 任务 | 触发时机 | 做什么 | 输出 |
|------|----------|--------|------|
| **Bootstrap** | 项目创建时 | 直接从 origin 尝试达成 goal | Fact + 可能的 Complete |
| **Reason** | 新 Fact / 新 Hint / Intent 清空 | 读完整图谱，判断：目标达成了吗？下一步该探索什么？ | Complete / 新 Intent[] / no-op |
| **Explore** | 有 unclaimed Intent 时 | 认领一个 Intent，执行探索 | 一个 Fact |

## 调度循环

```
每 N 秒一轮：
  1. 拉取所有 active 项目
  2. 检查 reason 触发条件（新 fact / 新 hint / intent 清空）
  3. 新项目 → 创建 bootstrap intent → 派发 Bootstrap
  4. 有 unclaimed explore intent → 派发 Explore
  5. reason 条件满足 → 派发 Reason
  6. 收割完成的任务 → 写回 Server
```

## Worker 角色

Worker **不直接**认领 Intent、不 heartbeat、不调 Server API。它只做一件事：

```
接收 Dispatcher 渲染好的 prompt → 在当前环境执行 → 返回结构化 JSON
```

Dispatcher 代 Worker 完成所有协议操作（claim、heartbeat、release、conclude）。

## 双阶段超时策略

Bootstrap 和 Explore 各有两个阶段：

```
Phase 1: execute (300s)
  → 正常完成 → 解析 JSON → 写 Fact
  → 超时/解析失败 → Phase 2

Phase 2: conclude (90s)
  → 同 session 继续 → 给模型"别探索了，总结一下" → 产出 Fact
  → conclude 也失败 → 释放 Intent → 任务失败
```

## 原始 Worker 后端

| Driver | 说明 |
|--------|------|
| `claudecode` | Claude Code CLI |
| `codex` | OpenAI Codex CLI |
| `pi` | pi-coding-agent CLI（我们保留的） |
| `mock` | 测试用假 driver |

每个项目跑在独立 **Docker 容器**中，Worker 在容器内执行。

## 关键设计原则（来自原 spec）

1. Agent 只接收 prompt、返回结构化结果；Dispatcher 是唯一协议写入者
2. 日志按"状态变化优先"——稳定轮询不刷屏，关键事件必须可见
3. Agent 通过黑板的图来协调（Stigmergy），不直接通信
4. 单 Dispatcher 实例，不支持多实例
5. 项目非 active 后立即取消所有运行中任务
6. Intent 不保留 worker 历史（只知道当前 claim 持有者）
