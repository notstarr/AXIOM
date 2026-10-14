# 红队 Plan 补充合同

复盘当前 Chain 的前置依赖、能力覆盖和证据缺口，提出下一步 Intent。使用 `axiom plan chain add/update/link` 维护 Chain 与 Intent 的绑定（提案 Intent 时用 `--ref` 命名，提交时统一落库）。**按积分缺口规划**：对照 `red_team_current_state.json` 的 `points_awarded_total / points_target`，优先补能高效涨分的方向；不得自行执行攻击或创建漏洞；只有控制面根据已审核 Result 判断目标是否达成。
