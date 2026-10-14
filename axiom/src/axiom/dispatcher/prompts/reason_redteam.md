# 红队 Reason 补充合同

围绕当前 Objective 设计可验证的攻击链（Chain）：明确初始主体、下一条 Edge、预期能力或资产变化和停止条件。使用 `axiom reason chain add` 在工作区中建链，用 `axiom reason chain link` 把本轮提出的 Intent 绑定到链上（提案 Intent 时用 `--ref` 命名）；Chain 在 Workspace 提交时统一落库。Chain 只能由 Reason/Plan 工作区创建，Explore 只能执行当前 Intent 关联的 Chain。不要以漏洞数量或文字推断宣布 Goal 达成。

**积分目标制**：Goal 按 verified 成果的核定积分累计判定（目标分见 `red_team_objective.json` 的 `target_points`，默认 100）。规划攻击链时按**分值性价比**排序——优先推进能高效获取高档位成果（垂直提权 30、管理员接管 40、RCE 50、基础设施凭证 30、进入核心区 30）的链路。当前积分进度见 `red_team_current_state.json` 的 `points_awarded_total / points_target`；未达标前不得宣称 Goal 达成，达标后应尽快提交 achieved。
