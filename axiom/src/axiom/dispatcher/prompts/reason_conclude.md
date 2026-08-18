# AXIOM 推理规划收口（Reason Conclude）

当前 ReasonRun 已进入有界收口。**禁止进行网络探测**，只允许通过 Axiom CLI 检查并修正 Workspace，补齐未完成的评估与操作。

- Task：`{task_id}` | ReasonRun：`{reason_run_id}` | Workspace：`{workspace_id}`
- Target 规则：{target_guidance}

## 收口动作清单

1. **读取判定基准**：运行 `axiom skill path whatisvul` 并完整读取 `whatisvul/SKILL.md`（排障：`axiom skill doctor whatisvul`）。
2. **检查状态**：运行 `axiom reason status`，查看当前工作区有哪些校验未通过（如缺少 goal assess、未消费 Fact、未评估 Intent）。
3. **评估未决 Intent**：对 `assessment_status=open` 的 Intent 执行 `axiom reason intent assess --intent-id iXXX --status completed|partial|blocked --reason "..."`。
4. **补齐 Todo / 派发未完分支**：对仍有未测分支的能力树保留 `open`/`partial` Todo 并派发新 Intent；严禁将未测完的模块直接改为 `completed`。
5. **提交 Goal Assessment（关键）**：
   - 若未达成或仍有后续测试：`axiom reason goal assess --not-achieved --reason "理由..." --evidence "fact:fXXX"`
   - 若已达成：`axiom reason goal assess --achieved --reason "理由..." --evidence "fact:fXXX"`
   - **注意**：若评估后又修改了 Todo/Intent，必须重新调用一次 `goal assess`。
6. **再次验证**：重新运行 `axiom reason status` 确认 `ready=true`。

## 结束协议

确认工作区就绪后，最后一条助手消息必须严格只有：

FINISH

禁止反引号、代码块、句号、解释、总结或其它任何字符。
