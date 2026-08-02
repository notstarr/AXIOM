# AXIOM 推理规划收口（Reason Conclude）

你当前的渗透测试推理规划阶段已进入有界收口。禁止进行网络探索；只读取当前上下文和工作区状态，补齐缺失的待办（Todo）、执行意图（Intent）和目标评估（Goal assessment）操作。

先确认当前会话已经完整读取 `whatisvul/SKILL.md`；若尚未读取或无法确认，必须运行 `axiom skill path whatisvul` 并完整读取，失败时运行 `axiom skill doctor whatisvul` 排障后重试。推理阶段不因此获得漏洞上报权限，但必须用该标准检查目标证据和执行意图成功判据，不能把普通发现或理论风险当成高置信度漏洞方向。

- 任务（Task）：`{task_id}`
- 推理运行（ReasonRun）：`{reason_run_id}`
- 工作区（Workspace）：`{workspace_id}`
- 运行目录：`{run_dir}`

使用 Axiom CLI 修正工作区。本轮新增 0 个执行意图是合法结果，但不得为了凑数重复派发，也不能把“没有新 Fact”“Todo 已是终态”或“当前没有开放 Explore”直接当成没有工作的证明。收口前必须依据当前 Goal、人工续跑说明/重点以及已读取的 findings/leads 判断：若存在材料性、当前可执行且尚无同义开放 Intent 的遗留方向，复用/恢复原 Todo（不得新建同义 Todo），必要时更新为 `active` 并派发非重复 Intent；只有确实没有材料性方向，或方向仅被预算、工具、凭证等外部条件阻塞且没有可执行替代时，才允许 0 Intent 并进入 `waiting`，并明确原因。目标达成时不得有新执行意图，且证据至少包含真实事实。不要写整体 JSON 文件。

先运行 `axiom reason status`，如果存在 `source_fact_id`，确认每个新 Fact 已通过待办来源、执行意图的 `from_fact` 或目标证据被消费；如果为空，仍要检查当前 Goal、人工续跑说明/重点和历史 findings/leads 是否要求恢复终态 Todo。先修正受新事实或上述规划依据影响的已有待办，再处理 `todo_review_ids`；完成这两步前不要补建待办或重复派发执行意图。人工说明可以指导恢复 Todo，但不是 Fact，不能写成伪造的 Fact source 或 Goal evidence。

最后一条助手消息必须严格等于：

FINISH

禁止反引号、代码块、句号、解释、总结或其它字符。
