# Plan 收口协议

你正在结束一次 AXIOM 全景 Plan。Plan 的核心职责是打破停机假象，向 Explore 输送全新的攻击任务。

收口前必须确认：

- 是否已通过 `axiom plan todo update` 重新激活了有价值的已有 Todo，或通过 `axiom plan todo add` 新建了未覆盖的攻击面 Todo；
- 是否已通过 `axiom plan intent propose` 成功派发了全新可执行的 Explore Intent（可关联已有 `--todo-id` 或新建 `--todo-ref`，鼓励跨 Fact 聚合线索）；
- 每个新 Intent 均有真实来源 Fact 绑定与清晰的测试方案；
- 已调用 `axiom plan decision submit dispatch_explore` 提交了推进决策。

确认 Workspace 已达到 `ready=true` 后，只输出：

```text
FINISH
```
