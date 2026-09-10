# 任务图谱 UI/UX 改造方案（v2：色系不动，纯设计层）

> 范围：**纯前端设计/排版层**，零功能新增，**色系保持现有 per-type 配色不变**。
> 实体模型：origin / goal / intent / fact 四种实体（9 个渲染变体：origin、depth_origin、goal、goal_history、fact、depth_fact、depth_plan、bootstrap_pending、bootstrap_running）。
> 学的是 artex 的 **UI/UX 设计**：卡片信息层级（图标区/标题/内容/状态徽章/ref 编号的排版结构）、点阵背景、选中态、minimap 排版——不是它的配色。
> 涉及文件仅 2 个：`static/custom.css`（68 条 node-card 规则的宿主）、`static/js/graph.js`（nodeCardTpl :130-152、尺寸 :132-133、minimap :773-840、cy style 块 :299-460）。

---

## 一、节点卡片重设计（核心，custom.css + graph.js:130-152）

### 1.1 尺寸（graph.js:132-133）

现状：`w = data.width || 60; h = data.height || 40` → 60×40 小卡片，信息装不下。

改：默认 **200×76**。各节点构造处已显式传 `nodeSize.width/height`（openIntentNodeSize 等），同步检查这些尺寸函数返回值，与 200×76 对齐（宽度不足的任务书卡允许 240）。

### 1.2 卡片排版结构（nodeCardTpl 重排——保留全部现有类名与数据引用）

现状 header/body 两段 → artex 式四层信息：

```
┌────────────────────────────────┐
│ (图标) 标题首行…      [状态pill] │  header
│ 正文内容 2 行截断……             │  body
│                      #i056     │  ref 角标
└────────────────────────────────┘
```

改动点（全部在现有 `nodeCardTpl` 内）：

1. **图标区**：现有 11px 裸 SVG（`nodeIconSvg`）→ 16px 居中放在固定宽图标槽（不改 SVG 本体，只加槽位排版）；图标颜色沿用现有 per-type header 色（色系不动）
2. **标题**：单行截断（`text-overflow:ellipsis; white-space:nowrap`）
3. **状态徽章**：现有 `node-card__status`（goal 专用"✓ 已完成"）→ **通用化**：intent/fact 节点的 `status` 字段数据已存在（edge[status] 同源），映射为 pill 展示——纯展示既有数据

   ```js
   const STATUS_PILL = {
     completed: ['is-done', '已完成'], running: ['is-run', '进行中'],
     in_progress: ['is-run', '进行中'], claimed: ['is-run', '进行中'],
     pending: ['is-queue', '排队'], blocked: ['is-block', '受阻'],
     failed: ['is-fail', '失败'],
   };
   ```
4. **ref 角标**：`<span class="node-card__ref">#{data.id}</span>`——data.id 已有（i056/f056/a001），右下角 fixed 排版
5. 保留：`interventionDot`、`reason-beacon`、`archived` 类、`goalStatus` 逻辑（并入通用 pill 映射）

### 1.3 卡片样式（custom.css 修改既有规则）

```css
.node-card { border-radius: 6px → 10px;
  box-shadow: 0 1px 2px rgba(15,23,42,.06), 0 3px 10px rgba(15,23,42,.05);
  padding: 8px 10px; }                     /* 现有 flex/pointer-events 不动 */
.node-card__body { display:-webkit-box; -webkit-line-clamp:2;
  -webkit-box-orient:vertical; overflow:hidden; }
.node-card__ref { position:absolute; right:8px; bottom:5px;
  font-size:9px; font-family:ui-monospace; opacity:.55; }
```

per-type 的 border/header 色规则**全部保留原值**（色系不动）；仅当某 type 的新排版需要 padding/字号微调时改排版参数。

状态 pill 配色（新增 5 条，中性的——不属于类型色系）：

```css
.node-card__status.is-done { background:#d1fae5; color:#047857; }
.node-card__status.is-run  { background:#dbeafe; color:#1d4ed8; }
.node-card__status.is-queue{ background:#f1f5f9; color:#475569; }
.node-card__status.is-block{ background:#fef3c7; color:#b45309; }
.node-card__status.is-fail { background:#fee2e2; color:#b91c1c; }
```

### 1.4 选中态（graph.js cy style 块）

```js
{ selector: ':selected', style: { 'border-width': 2.5, 'border-color': '#3b82f6' } }
```

HTML 卡浮于节点上方，节点 border 加粗即透出选中描边。

---

## 二、画布背景点阵（custom.css 新增一处）

```css
#cy { background-color:#fafbfc;
  background-image: radial-gradient(circle, #e5e9f0 1px, transparent 1px);
  background-size: 22px 22px; }
:root.dark #cy { background-color:#0f172a;
  background-image: radial-gradient(circle, #1e293b 1px, transparent 1px); }
```

---

## 三、连线排版微调（graph.js:385-460 style 块）

- `curve-style: bezier` 已全部在 ✓ 不动
- 线宽对比拉开：活跃（intent_completed/goal_evidence）2 → **2.5**；辅助（in_progress/unclaimed/cancelled）1.5 → **1.2**
- `arrow-scale` 统一 0.8
- 颜色全部不动（状态分色是我们自己的语义）

---

## 四、minimap 排版对齐（graph.js:773-840）

- 节点方块颜色：沿用现有 per-type 色（**不改色**），仅统一尺寸/间距绘制
- 背景白、容器圆角 8px、主视口指示框描边 `#3b82f6` 40% 透明
- 全部在 canvas 绘制函数内改参数

---

## 五、明确不做

| 排除项 | 理由 |
|---|---|
| 色系重设计 | 用户裁定：色系不动 |
| 交互式类型过滤 checkbox | 新功能 |
| 换布局引擎 / React Flow | 架构重写无收益 |
| facts ref 注入 | 7.5 已裁定 |

---

## 六、验证

1. `node --check graph.js` + `index.html` 的 `?v=` bump（graph.js 与 custom.css）
2. 视觉走查：t_204 图谱逐项对照（点阵/卡片排版/状态 pill/ref 角标/选中描边/minimap）
3. 交互回归：节点点击（fact 链接跳转）、详情面板联动、minimap 拖拽、缩放——零回归
4. 性能：t_188 量级 500+ 节点帧率不退化（纯样式，节点数不变）

## 工作量

graph.js（模板+尺寸+minimap+选中）+ custom.css（排版/pill/背景）≈ 半天，一次提交。

---

## v3 增补（实机反馈后的问题修正 + artex 版式/曲线对齐）

> 实机暴露三类问题：① fact 卡正文 = 审计文本（刀③a 的设计回归——卡片没有独立内容源）；② 卡片版式与 artex 不符（类型名应为标题、图标为方圆角彩底、状态 pill 与 ref 应在底部一行）；③ 连线是直线斜穿，不是 artex 的 S 曲线；④ 边标签是 100 字描述截断，不是关系词。

### v3-1 fact 卡内容源修正（graph.js buildElements fact 节点段 :18-34）

- 现状：fact 节点 `description: f.description` → nodeCardTpl body 直接渲染审计文本
- 改：body 数据源改为**来源 intent 的标题**——fact_provenance 已含 source_intent_ids，取首个 explore intent 的 `title`（数据已在 project.intents）；fact.description 保留在详情面板（点开可见完整审计记录）
- 语义：卡片显示"这个 fact 来自哪次探索、做了什么"，详情才是审计记录

### v3-2 卡片版式 v3（对齐 artex 逐元素）

| 元素 | artex | 我们改法 |
|---|---|---|
| 图标 | 方圆角彩底（18×18）+ 白色图形 | `.node-card__type-icon` border-radius 9999px→6px，18×18 保持 |
| 标题 | **类型名粗体**（"意图"/"目标"） | header 首行 = 类型名（nodeTitle 已有），正文行才是内容 |
| 内容 | 1-2 行正文 | body 保持 2 行 clamp |
| 状态 pill | 底部左（含状态点 ●） | pill 从 header 移到**底部行左侧**（新增 footer 行） |
| ref | 底部右 `#i3` | 保持底部右 |
| 选中 | 蓝描边 | 已实现 ✓ |

nodeCardTpl 结构 v3：

```
header: [icon][类型名]
body:   [内容 ≤2 行]
footer: [状态pill] ......... [#ref]
```

CSS：新增 `.node-card__footer { display:flex; align-items:center; margin-top:auto; }`；`__ref` 保持 absolute；`__status` 移入 footer（去 margin-left:auto，footer 用 gap）。

### v3-3 连线 S 曲线（graph.js style 块 + layoutstop hook）

- 现状：`bezier` 在长距离跨列边上退化为直线斜穿
- 改：`curve-style: 'unbundled-bezier'` + **控制点**——源右边缘水平中点、目标左边缘水平中点两个控制点形成 S 弯
- 控制点计算：layoutstop 后按源/目标位置重算（vendor 的 dagre/elk 布局已定坐标，控制点用 `edges.forEach` 设置 `edge.style('control-points', [...])` 或元素 data 预置权重）
- 兜底：若 unbundled-bezier 控制点坐标系实现成本超预期，退级 `curve-style: 'taxi'`（圆角正交弯，视觉接近 S 曲线的 80%）——实施时二选一
- 边标签：`label` 从 100 字描述截断 → **关系短词**（派生/产出/证明，数据已有 edgeType/来源类型）；完整描述保留在 hover tooltip（`title` 属性已传）

### v3-4 验证

- 渲染走查：fact 卡显示来源 intent 标题；卡片三行版式（header/footer/ref）与 artex 截图逐元素对照
- 曲线走查：跨列边呈 S 弯；同列短边不畸形（控制点按距离缩放）
- 交互回归：fact 链接跳转/详情面板/minimap 零回归（链接解析基于文本，与卡片版式解耦 ✓）
