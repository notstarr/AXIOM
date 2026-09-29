# `duplicate` 裁决理由示例（不产出报告）

> 除「确认成立」外，其余裁决**不产出报告**，只在裁决 `--reason` 里说明（1-3 句，精炼直击要害）。

**场景**：v0xx 与已存在的 v0yy 同根因 —— 同一 `/api/orders/{id}` 未做归属校验，仅入口路径不同。

**裁决命令**：

```bash
axiom review decision submit duplicate --duplicate-of v0yy --reason "与 v0yy 同根因：同一 /api/orders/{id} 未做归属校验，仅入口路径不同，不重复计数。"
```

**要点**：必须给出**同根因证据**（同一代码路径 / 同一校验缺失），不能仅凭标题相似判重。
