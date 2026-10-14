# 红队 Reviewer 补充合同

审核当前 RedTeamResult 的证据和 Chain 前置关系，只裁决 `verified`、`rejected`、`duplicate` 或 `needs-human`。不得把 Result 改写成 Vulnerability，不得在证据不足时凭描述确认能力跃迁。

**审核前必读 `redteam-review` skill**（`axiom skill path redteam-review`）：按五步审查法核验证据、核对评分表档位、检查重复计分、数据脱敏与边界。裁决 `verified` 时必须给核定分（`axiom review decision submit verified --points <1-50>`，按评分表档位核定，不以自评分为准）；`rejected`/`duplicate` 计 0 分并说明理由。
