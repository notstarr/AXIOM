# 两种模式动画触发逻辑

## 图谱扫描线动画

### 触发条件

```
project.project.reason !== null
```

只要 `project.reason` 字段不为空，扫描线动画就开启。**不分广度深度，共用同一套逻辑。**

### 如何被设置

| 操作 | 设置者 |
|------|--------|
| reason 任务 dispatch → 调 `POST /projects/{id}/reason/claim` | `HeartbeatLease.for_reason()` |
| reason 任务结束 → 调 `POST /projects/{id}/reason/release` | `best_effort_release_reason()` |

### 如何被清除

- 调度器在 reason 任务结束时主动调 release
- 服务端定期 `expire_workers` 清理超时的 reason 心跳

### 当前问题

plan 阶段不应该触发扫描线，但因为：
1. plan 结束后 reason 立刻被 dispatch（修复前）
2. 旧的 reason lease 残留（重启后未清理）

导致 plan 阶段也出现扫描线。

---

## 右上角状态面板

### 触发条件

```
project.project.reason !== null
```

### 显示内容

| 模式 | 标签文字 | 颜色 | worker 来源 |
|------|---------|------|------------|
| 广度 `depth_mode=false` | "reason 运行中" | 天蓝 | `project.reason.worker` |
| 深度 `depth_mode=true` | "plan 运行中" | 琥珀 | `project.reason.worker` |

### 当前问题

深度模式显示 "plan 运行中"，但 `worker` 字段来自 `project.reason.worker`——这是 reason lease 的 worker，不是 plan 任务的 worker。所以可能出现：**标签说 plan，但 worker 名是上一次 reason 用的 worker。**

---

## intent 节点脉动

### 触发条件

正在运行的 explore intent（`worker` 不为空 且 `to` 为空）：

```javascript
openIntentNodeType(intent) {
  return intent.worker ? 'in_progress' : 'unclaimed';
}
```

### 动画

| 节点类型 | CSS class | 动画 |
|---------|-----------|------|
| `in_progress` | — | 橙色椭圆 + `intentChipGlow` 1.8s 脉动 |
| `unclaimed` | — | 灰色虚线椭圆，无动画 |

---

## 任务列表 chip 脉动

### 触发条件

```html
<span :class="intent.worker ? 'intent-chip-running' : ''">
```

任务列表中的 intent chip：**有 worker 在跑 → 脉动动画**。

reason chip 同理：`project.project.reason !== null` → `reason-chip-running`。

---

## 工具卡片淡入

操作回溯页新增工具调用记录时：

```css
.tool-card-enter { animation: toolCardIn 0.3s ease-out; }
```

---

## 时间线闪入

```css
.timeline-flash { animation: timelineFlash 1s ease-out; }
```

---

## 总结：动画开关一览

| 动画 | 开关条件 | 广度 | 深度 |
|------|---------|------|------|
| 图谱扫描线 | `project.reason !== null` | ✅ | ❌ bug (plan后reason抢跑) |
| 右上角面板 | `project.reason !== null` | ✅ (reason运行中) | ✅ (plan运行中) |
| intent 脉动 | explore 有 worker | ✅ | ✅ |
| task chip 脉动 | intent.worker | ✅ | ✅ |
| reason chip 脉动 | project.reason | ✅ | ✅ |
| 工具卡片淡入 | 新记录 | ✅ | ✅ |
| 时间线闪入 | 新事件 | ✅ | ✅ |
