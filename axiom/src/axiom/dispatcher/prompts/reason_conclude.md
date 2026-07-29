# AXIOM 推理规划收口（Reason Conclude）

当前推理规划阶段已进入有界收口。不要重新进行网络探索，也不要上报漏洞；只读取当前上下文和工作区状态，补齐缺失的待办（Todo）、执行意图（Intent）和目标评估（Goal assessment）操作。

先确认当前会话已经完整读取 `whatisvul/SKILL.md`；若尚未读取或无法确认，必须运行 `axiom skill path whatisvul` 并完整读取，失败时运行 `axiom skill doctor whatisvul` 排障后重试。推理阶段不因此获得漏洞上报权限，但必须用该标准检查目标证据和执行意图成功判据，不能把普通发现或理论风险当成高置信度漏洞方向。

- 任务（Task）：`{task_id}`
- 推理运行（ReasonRun）：`{reason_run_id}`
- 工作区（Workspace）：`{workspace_id}`
- 运行目录：`{run_dir}`

使用 Axiom CLI 修正工作区。本轮新增 0 个执行意图是合法结果：没有材料性的新方向时，不得为了凑数重复派发；如果当前也没有开放探索执行意图，先将 `todos.json` 中所有无后续工作的 `pending/active` 待办收口为 `completed/rejected/obsolete`，再提交目标未达成的评估，任务将进入 `waiting`。目标达成时不得有新执行意图，且证据至少包含真实事实。不要写整体 JSON 文件。

先运行 `axiom reason status`，确认每个 `source_fact_id` 已通过待办来源、执行意图的 `from_fact` 或目标证据被消费；先修正受新事实影响的已有待办，再处理 `todo_review_ids`。完成这两步前不要补建待办或重复派发执行意图。

最后一条助手消息必须严格等于 `FINISH`，不得附带任何说明。
