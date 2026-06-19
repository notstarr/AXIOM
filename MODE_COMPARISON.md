# 广度模式 vs 深度模式 对比

## 阶段概览

| | 广度模式 | 深度模式 |
|--|---------|---------|
| 第一阶段 | **boot** | **plan** |
| 第二阶段 | reason | reason |
| 第三阶段 | explore | explore |
| prompt 目录 | `prompts/default/` | `prompts/depth/` |
| 输入 | origin: URL | origin: 流量数据/HAR |
| 停止 | goal 达成 或 连续10轮无高危 | 清单全部完成 |

---

## 第一阶段：boot vs plan

| | boot | plan |
|--|------|------|
| **做什么** | 拿 URL 自由探索，尝试直接达成 goal | 分析流量数据，产出测试清单 |
| **产出** | fact（探索发现）或 complete | checklist.md + sequence.md + 一个汇总 fact |
| **有 goal 吗** | ✅ 有，goal 驱动探索 | ❌ 没有 goal |
| **有 conclude 吗** | ✅ 超时后总结 | ❌ 单阶段 |
| **注入变量** | `{origin}` `{goal}` `{hints}` | `{origin}` `{checklist_path}` `{sequence_path}` |
| **prompt 文件** | `default/bootstrap.md` | `depth/plan.md` |

---

## 第二阶段：reason

| | 广度 reason | 深度 reason |
|--|-----------|-----------|
| **做什么** | 读全图 → 发散思考 → 提新方向 | 读 checklist + sequence → 逐条对比 → 补未测的 |
| **输入** | graph_yaml + fact_ids + open_intents | graph_yaml + `{checklist_path}` + `{sequence_path}` + `{plan_fact_id}` |
| **方向来源** | 模型自己根据事实发散 | plan 的 checklist，不新增方向 |
| **intent 数量** | 限 `max_intents` 个 | 不限，有多少未测补多少 |
| **判 goal** | ✅ 判断 goal 是否达成 | ❌ 不判 goal |
| **noop 行为** | 返回成功，继续循环 | complete 项目 |
| **complete** | goal 达成 → complete | 清单全测完 → complete |
| **prompt 文件** | `default/reason.md` | `depth/reason.md` |

---

## 第三阶段：explore

| | 广度 explore | 深度 explore |
|--|------------|------------|
| **做什么** | 认领一个 intent，探索方向，产出事实 | 同左 |
| **注入变量** | `{graph_yaml}` `{intent_id}` `{intent_description}` | 同上 + `{ai_dir}` `{checklist_path}` `{sequence_path}` |
| **凭据来源** | 从图 fact 中找 | 读 checklist.md（token/cookie） |
| **API 构造** | 自己从图 fact 中提取 | 读 checklist.md + sequence.md |
| **skill 提醒** | ✅ "check available_skills" | ✅ 同左 |
| **文件产出** | 无约束 | 只能写 `{ai_dir}/` |
| **有 conclude** | ✅ 超时后总结 | ✅ 同左 |
| **prompt 文件** | `default/explore.md` | `depth/explore.md` |

---

## 图谱显示

| | 广度 | 深度 |
|--|------|------|
| origin 颜色 | 青色 `#14b8a6` | 青色 `#14b8a6` |
| goal 颜色 | 红色 `#f43f5e` | **不显示** |
| plan 产出 fact | — | 黑色 `#1e293b` (`depth_plan`) |
| explore 产出 fact | 紫色 `#6366f1` (`fact`) | 黄色 `#facc15` (`depth_fact`) |
| 形状 | round-rectangle | round-rectangle |

---

## Worker 配置

| | 广度 worker | 深度 worker |
|--|-----------|-----------|
| task_types | `[boot, reason, explore]` | `[plan, reason, explore]` |
| 可共用吗 | ✅ 可以指向同一模型 | ✅ 同左 |

---

## 配置管理

| 配置项 | 广度 | 深度 |
|--------|------|------|
| 阶段并发 | boot / explore / reason | plan / explore / reason |
| 阶段超时 | boot / boot总结 / explore / explore总结 / reason | plan / explore / explore总结 / reason |
| 共用 | 调度并发、心跳与健康检查 | 同左 |
