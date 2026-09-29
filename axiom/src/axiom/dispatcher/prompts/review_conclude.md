# AXIOM 漏洞审核收口（Review Conclude）

Task：`{task_id}` | ReviewRun：`{review_run_id}` | 漏洞：`{vulnerability_id}`

1. 检查 `{run_dir}/artifacts/`：**不要**再写 `review.md`（已废除）。裁决为「确认成立」的漏洞，其漏洞报告 `vuln_report-<vuln_id>-<slug>.md` 必须存在且非空（文件名纯 ASCII）；其余状态不产出报告。
2. **不修改漏洞原始正文**（标题、描述、等级、影响、修复建议一律不改写），只提交结构化裁决。不得仅根据旧 Finding/Evidence 强行提交。
3. 如需参考裁决示例，按 `references/*-example.md` 规则读取。
4. 若尚未提交裁决，调用 `axiom review decision submit verified|duplicate|ignored|needs-human --reason "..."`（原因精炼直击要害，1-3 句）。
5. 运行 `axiom review status` 确认就绪。

最后一条助手消息必须严格只有：

FINISH

禁止反引号、代码块、句号、解释和总结。
