---
name: redteam-review
description: 红队成果审核前必读，包含五步审查法、核定分规则与示例
---

# RedTeam review（红队成果审核）

本 Skill 只给 **Reviewer** 阶段使用。审核对象是红队成果（RedTeamResult）：每条成果自带上报者的自评分（`points_declared`），你的职责是**核定分**（`points_awarded`）——落库以核定分为准。

## 五步审查法

1. **证据核验**：声明的 capability 变化必须有对应请求/响应或命令回显证据；无证据 → 降档或驳回
2. **分数核对**：对照评分表核档（权限 10/15/30/40/50、数据 10/20/25、凭证 10/25/30、横向 20/30）；自评分高于证据支撑的档位 → 下调到对应档
3. **重复计分**：同任务同链同 capability 是否已有 verified 成果 → 有则标 duplicate（计 0 分）
4. **数据脱敏**：data 类样本不得出现明文敏感值；字段必须在白名单内；样本数/字节数超限驳回
5. **边界检查**：成果是否越任务 Scope / Objective（任务显式声明了能力/资产上限时）

## 核定规则

- `verified`：必须给核定分 `--points`（1-50），落库以核定分为准
- `rejected`：证据不成立，0 分，理由必须说明缺什么
- `duplicate`：重复计 0 分，说明与哪条成果重复
- `needs-human`：需要人工确认（如需要真实短信验证码），0 分

## 示例

verified（核定 30 分——证据支撑垂直提权档位）：

```bash
axiom review decision submit verified \
  --reason '管理接口调用证据完整：普通成员会话成功调用 promote 接口且返回 200，符合评分表垂直提权档（30 分）。自评 30 分一致。' \
  --points 30
```

rejected（证据不足）：

```bash
axiom review decision submit rejected \
  --reason '仅提供目标接口的存在性证明，无成功调用管理员功能的请求/响应，不满足垂直提权认分条件。'
```

duplicate（重复计分）：

```bash
axiom review decision submit duplicate \
  --reason '与 rr000003 为同一 capability（tenant:admin）的重复成果，按规则计 0 分。'
```
