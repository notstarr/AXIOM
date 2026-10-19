# 红队 Plan 收口

停止规划外的动作。确认 Workspace 里的链和 Intent 已经落好。

- 开放的链已经挂了 Intent，或已改成 blocked、completed、abandoned。
- 新 Intent 使用 `--chain-id` 或 `--chain-ref`。
- 已提交 `axiom plan decision submit`。
- `axiom plan status` 为 `ready=true`。

最后一条助手消息只能是：

FINISH
