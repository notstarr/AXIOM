# AXIOM Reason 收口

当前 Reason 已进入有界收口。不要重新进行网络探索，也不要上报漏洞；只读取当前 Context 和 Workspace 状态，补齐缺失的 Todo/Intent/Goal assessment 操作。

- Task：`{task_id}`
- ReasonRun：`{reason_run_id}`
- Workspace：`{workspace_id}`
- 运行目录：`{run_dir}`

使用 Axiom CLI 修正 Workspace：Goal 未达成时至少保留一个可执行 pending Explore Intent，Goal 达成时不得有新 Intent 且 evidence 至少包含真实 Fact。不要写整体 JSON 文件。

最后一条 assistant 文本必须严格等于 `FINISH`，不得附带任何说明。
