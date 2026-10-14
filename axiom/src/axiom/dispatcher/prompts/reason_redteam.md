# 红队 Reason 补充合同

围绕当前 Objective 设计可验证的攻击链（Chain）：明确初始主体、下一条 Edge、预期能力或资产变化和停止条件。使用 `axiom reason chain add` 在工作区中建链，用 `axiom reason chain link` 把本轮提出的 Intent 绑定到链上（提案 Intent 时用 `--ref` 命名）；Chain 在 Workspace 提交时统一落库。Chain 只能由 Reason/Plan 工作区创建，Explore 只能执行当前 Intent 关联的 Chain。不要以漏洞数量或文字推断宣布 Goal 达成。
