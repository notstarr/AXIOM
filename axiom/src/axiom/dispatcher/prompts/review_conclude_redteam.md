# 红队成果审核收口

Task：`{task_id}` | ReviewRun：`{review_run_id}` | 成果：`{result_id}`

1. 只提交成果裁决和核定分。
2. 若尚未提交裁决，调用 `axiom review decision submit verified --points <分> --reason "..."`，或 `rejected`、`duplicate`、`needs-human`。分值按 `redteam-scoring`。
3. 运行 `axiom review status` 确认就绪。

最后一条助手消息只能是：

FINISH
