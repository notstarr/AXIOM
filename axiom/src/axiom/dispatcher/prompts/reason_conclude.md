# AXIOM Reason 收口

当前 Reason 已进入有界收口。不要重新进行网络探索，也不要上报漏洞；只读取当前 Context 和 Workspace 状态，补齐缺失的 Todo/Intent/Goal assessment 操作。

先确认当前会话已经完整读取 `whatisvul/SKILL.md`；若尚未读取或无法确认，必须运行 `axiom skill path whatisvul` 并完整读取，失败时运行 `axiom skill doctor whatisvul` 排障后重试。Reason 不因此获得漏洞上报权限，但必须用该标准检查 Goal 证据和 Intent 成功判据，不能把普通发现或理论风险当成高置信度漏洞方向。

- Task：`{task_id}`
- ReasonRun：`{reason_run_id}`
- Workspace：`{workspace_id}`
- 运行目录：`{run_dir}`

使用 Axiom CLI 修正 Workspace：Goal 未达成时至少保留一个可执行 pending Explore Intent，Goal 达成时不得有新 Intent 且 evidence 至少包含真实 Fact。不要写整体 JSON 文件。

最后一条 assistant 文本必须严格等于 `FINISH`，不得附带任何说明。
