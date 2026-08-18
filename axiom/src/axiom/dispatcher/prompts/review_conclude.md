# AXIOM 漏洞审核收口（Review Conclude）

Task：`{task_id}` | ReviewRun：`{review_run_id}` | 漏洞：`{vulnerability_id}`

立即停止扩展测试，完成审核收口：
1. 检查并补齐 `{run_dir}/artifacts/review.md`（不得为空，记录独立验证与对照）。
2. 若尚未提交裁决，调用 `axiom review decision submit verified|duplicate|ignored|needs-human --reason "..."`（原因精炼直击要害，1-3 句）。
3. 运行 `axiom review status` 确认就绪。

最后一条助手消息必须严格只有：

FINISH

禁止反引号、代码块、句号、解释和总结。
