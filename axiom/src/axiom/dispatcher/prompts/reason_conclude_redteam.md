# 红队 Reason 收口

当前 ReasonRun 已进入收口。禁止网络探测。只通过 Axiom CLI 修正 Workspace。

- Task：`{task_id}` | ReasonRun：`{reason_run_id}` | Workspace：`{workspace_id}`
- Target 规则：{target_guidance}

1. 运行 `axiom reason status`，看缺什么校验。
2. 评估未决 Intent：`axiom reason intent assess --intent-id iXXX --status completed|partial|blocked --reason "..."`。
3. 开放的链要么挂了下一步 Intent，要么已改成 blocked、completed 或 abandoned。
4. 提交 `axiom reason goal assess --not-achieved` 或 `--achieved`。改完链或 Intent 后要重新评估。
5. 再跑 `axiom reason status`，确认 `ready=true`。

最后一条助手消息只能是：

FINISH
