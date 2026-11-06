# AXIOM 漏洞复测收口（Retest Conclude）

Task：`{task_id}` | ReviewRun：`{review_run_id}` | 漏洞：`{vulnerability_id}`

1. 复测**没有文件产物**：不要在 `{run_dir}/artifacts/` 下找报告，也不要写任何 `.md`。
2. **不改任何定档字段、不改漏洞状态、不调用 `axiom review description submit`**。复测只回答「现在还成不成立」，改写内容属于审核。
3. 若尚未提交结论，调用 `axiom review retest submit exists|fixed|inconclusive --reason "..."`（1-3 句，直击证据）。
4. 若结论已提交但 `axiom review status` 仍不 `ready`，按报错补一张 `kind=packet` 的报文图（`axiom review evidence attach`）——复测只要求至少一张，不要求每步一张。
5. 运行 `axiom review status` 确认就绪。

最后一条助手消息必须严格只有：

FINISH

禁止反引号、代码块、句号、解释和总结。
