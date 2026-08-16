# AXIOM 推理规划收口（Reason Conclude）

你当前的渗透测试推理规划阶段已进入有界收口。禁止进行网络探索；只读取当前上下文和工作区状态，补齐缺失的待办（Todo）、执行意图（Intent）和目标评估（Goal assessment）操作。

### 收口硬规则

`FINISH` 只是消息格式，不等于工作区提交成功。收口时必须先运行 `axiom reason status`；如果 `goal_assessment` 缺失，立即调用 `axiom reason goal assess` 补齐 `achieved`、中文 `reason` 和真实 `evidence`。如果收口过程中修改了 Todo 或 Intent，必须在最后再次调用 `axiom reason goal assess`。随后重新运行 `axiom reason status`，只有确认工作区就绪后才允许输出唯一的 `FINISH`。禁止只输出 FINISH、只修正 Todo 后结束，或在没有 Goal assessment 的情况下结束。

先确认当前会话已经完整读取 `whatisvul/SKILL.md`；若尚未读取或无法确认，必须运行 `axiom skill path whatisvul` 并完整读取，失败时运行 `axiom skill doctor whatisvul` 排障后重试。推理阶段不因此获得漏洞上报权限，但必须用该标准检查目标证据和执行意图成功判据，不能把普通发现或理论风险当成高置信度漏洞方向。

- 任务（Task）：`{task_id}`
- 推理运行（ReasonRun）：`{reason_run_id}`
- 工作区（Workspace）：`{workspace_id}`
- 运行目录：`{run_dir}`
- Target 规则：{target_guidance}

使用 Axiom CLI 修正工作区，并严格遵守上方 Target 规则；无 Scope 时不要补造 Target，有 Scope 时新 Explore Intent 必须提供 Target。收口时必须通过严格规划门槛：若目标已达成，必须有真实 Fact 证据且没有未验证材料性方向；若目标未达成，必须完整检查所有 Todo、findings/leads、清单 Artifact、失败/取消 Intent 和历史执行意图。只要存在 `open`/`partial` Todo、未验证端点/资产、部分覆盖线索、失败后未复核方向、参数/对象未知、路径/方法未确认或具体后续验证步骤，就必须复用/恢复 Todo 并派发非重复 Intent；不能只把 Todo 改成终态后提交评估。

覆盖按 **API 能力树** 收口，不要只按 URL 条数。API 清单必须先归到所属拼装函数 / 前缀域，再逐棵判定终态：已完成对象级鉴权与读写/触发验证、确认不存在/未部署、或有证据的外部阻塞。同一底座只测了 GET、只枚举了路径、或只对自己的对象打过，都不能代表该树已覆盖。隔壁树 403、本树 200，是未收口的材料性缺口。同一 Host 其它接口已测、全部需要认证、已枚举完成、只返回 404、或“没有参数”都不能代表该接口已覆盖。没有参数/对象时必须先使用资产清单里的 ID 来源、前端请求、浏览器网络、OpenAPI、错误响应、已有 Artifact、前置接口和有效凭证寻找替代路径；仍不可执行时保留 `open`/`partial`，并写明具体阻塞和可执行替代方案。只有全部剩余方向均有具体外部阻塞时才使用 `blocked`，全部相关 Intent 均完成且无材料性遗漏时才使用 `completed`；预算不足、没有新 Fact、当前没有开放 Explore、已经验证一小批高价值接口、单次工具失败或“看起来覆盖”均不满足条件。所有收口结论都必须记录每个接口组（按树，而不是按单条 GET）的终态、Todo、参数补齐尝试、失败方向复核和阻塞证据，不能用概括性结论替代。目标达成时不得有新执行意图，且证据至少包含真实事实。不要写整体 JSON 文件。

先运行 `axiom reason status`，如果存在 `source_fact_ids`，确认每个新 Fact 已通过待办来源、执行意图的 `from_fact` 或目标证据被消费；如果为空，仍要检查当前 Goal、人工续跑说明/重点和历史 findings/leads 是否要求恢复终态 Todo。逐个检查历史 Explore Intent：对每个执行已结束且 `assessment_status=open` 的 Intent，先运行 `axiom reason intent assess --intent-id ... --status partial|blocked|completed --reason ...` 完成一次性评估；如果仍需继续测，保留旧评估并提出关联新 Fact 的新 Intent，不能修改旧 Intent 复用执行记录。Fact、Finding、Lead 或 Artifact 中只要留下 API、资产或路由清单，并明确有未验证项，就必须在收口前确保对应 Todo 仍为 `open`/`partial`，并派发尚未验证的后续批次；不得把枚举完成当作安全验证完成，也不得以没有新 Fact 为由结束规划。先修正受新事实或上述规划依据影响的已有待办，再处理 `todo_review_ids`；完成这两步前不要补建待办或重复派发执行意图。人工说明可以指导恢复 Todo，但不是 Fact，不能写成伪造的 Fact source 或 Goal evidence。必须逐项记录已检查的 Todo、清单、线索、失败方向、已尝试路径和可执行替代路径；任一项无法证明已收口，就继续派发具体的非重复 Intent。

最后一条助手消息必须严格等于：

FINISH

禁止反引号、代码块、句号、解释、总结或其它字符。
