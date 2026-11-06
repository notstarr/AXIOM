# 红队成果审核收口

Task：`{task_id}` | ReviewRun：`{review_run_id}` | 成果：`{result_id}`

1. 只提交成果裁决和核定分。
2. 若描述类内容（描述、复现）需要改写，在收口前用 `axiom review description submit` 写好；上报已经写清楚就不必改。
3. 若尚未提交裁决，调用 `axiom review decision submit verified --points <分> --reason "..."`，或 `duplicate`、`ignored`、`needs-human`。分值按 `redteam-scoring`。
4. `duplicate` 表示「这条成立，但与另一条成果重复，同一资产只计一次」；`ignored` 表示「这条不成立」。两者不要混用，判重请用 `duplicate`。
5. 运行 `axiom review status` 确认就绪。

最后一条助手消息只能是：

FINISH
