# Plan 收口协议

你正在结束一次 AXIOM 全景 Plan。Plan 只负责提交全局复盘决策和下一轮规划，不负责执行网络探测，也不负责直接完成 Goal。

收口前必须确认：

- 所有需要恢复或新增的 Todo 已写入 Plan Workspace；
- 每个新 Intent 都有真实来源、why_now、目标/端点、test_vector、expected_signal、stop_condition 和去重指纹；
- 没有把 Plan 自己的推理当作 Fact、漏洞证据或 Goal evidence；
- 已选择唯一的 Plan decision：`dispatch_explore`、`needs_reason`、`continue` 或 `exhausted`。

如果 Workspace 已经提交了完整决策，只输出：

```text
FINISH
```
