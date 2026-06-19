# 前端动画与视觉效果

## 图谱节点

### 广度模式

| 节点 | 颜色 | 形状 | 动画 |
|------|------|------|------|
| origin | 青色 `#14b8a6` | round-rectangle | 无 |
| goal | 红色 `#f43f5e` | round-rectangle | 无 |
| fact | 紫色 `#6366f1` | round-rectangle | 无 |
| in_progress | 橙色 `#f59e0b` | ellipse | intent-chip-glow 脉动 |
| unclaimed | 灰色 `#cbd5e1` | ellipse (虚线) | 无 |

### 深度模式

| 节点 | 颜色 | 形状 | 动画 |
|------|------|------|------|
| origin | 青色 `#14b8a6` | round-rectangle | 无 |
| plan fact (f001) | **黑色 `#1e293b`** | round-rectangle | 无 |
| explore fact | **黄色 `#facc15`** | round-rectangle | 无 |
| goal | **不显示** | — | — |

## 图谱覆盖动画（reason 扫描线）

两张模式共用同一套 reason 扫描线动画，在 `project.project.reason` 不为空时触发：

| 元素 | 动画 | 周期 | 效果 |
|------|------|------|------|
| `.reason-graph-scan::before` | `reasonFramePulse` | 2.8s | 边框呼吸 |
| `.reason-graph-scan > span` | 扫描线标签内亮光 | — | 静态扫光 |
| `.reason-graph-scanline::before` | `reasonGraphScanline` | 4.2s linear | 横线从顶扫到底 |
| `.reason-graph-sweep` | `reasonGraphSweep` | 4.6s | 对角线扫光 |
| `.reason-graph-ambient` | `reasonAmbientPulse` | 2.4s | 整体明暗脉动 |
| `.reason-graph-grid` | — | — | 静态网格背景 |

## 右上角状态面板

| 模式 | 标签 | 颜色 | 动画 |
|------|------|------|------|
| 广度 | "reason 运行中" | 天蓝 `text-sky-500` | `reasonPanelPulse` (2.1s) |
| 深度 | "plan 运行中" | 琥珀 `text-amber-500` | 同上 |
| 信标点 | 红点 `.reason-beacon` | — | `reasonBeaconPulse` (1.8s) |

## 任务列表

| 元素 | 动画 | 效果 |
|------|------|------|
| reason chip | `reasonChipGlow` 1.8s | 天蓝色脉动 |
| intent chip | `intentChipGlow` 1.8s | 橙色脉动 |
| timeline entry | `timelineFlash` 1s | 新条目闪入 |

## 工具卡片（操作回溯）

| 动画 | 周期 | 效果 |
|------|------|------|
| `toolCardIn` | 0.3s | 新卡片从下方淡入 |

## 深度 vs 广度差异总结

| 维度 | 广度 | 深度 |
|------|------|------|
| fact 颜色 | 紫色 | 黄色 |
| plan 颜色 | — | 黑色 |
| goal 节点 | 红色，显示 | 不显示 |
| 扫描线动画 | ✅ | ❌ (暂无) |
| 右上角标签 | "reason 运行中" 天蓝 | "plan 运行中" 琥珀 |
| origin 颜色 | 青色 | 青色（同广度） |
