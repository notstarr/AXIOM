# AXIOM Reviewer 收口

Task：`{task_id}`

ReviewRun：`{review_run_id}`

漏洞：`{vulnerability_id}`

运行目录：`{run_dir}`

立即停止扩展测试，只完成审核合同：

1. 检查并补齐 `{run_dir}/artifacts/review.md`，不得为空。
2. 仅保留已经生成的 `{run_dir}/artifacts/evidence/**`，不要为了补证据启动新的大规模测试。
3. 若当前 Lease 分配了 `vuln-review`，确认已经读取候选裁决对应的 `references/*-example.md`；未读取时只补读该小型附件，不要重新加载无关 Skill 或大量上下文。
4. 检查审核记录是否按 CVSS v4.0 风格写明 `AV/AC/AT/PR/UI`、`VC/VI/VA`、`SC/SI/SA` 的证据或未知原因，并包含本 ReviewRun 的独立验证和关键对照。
5. 若没有独立验证，只有在记录了明确阻塞条件或“直接静态证据已完整、复测无新增价值”的具体理由时，才能提交终态裁决。
6. 若尚未完成必要验证且现在已无法安全补做，不得仅根据旧 Finding/Evidence 强行提交 `verified` 或 `ignored`；应提交 `needs-human` 并写明已尝试步骤和阻塞原因。
7. 如果尚未提交结构化裁决，依据上述规则调用一次 `axiom review decision submit`。
8. 运行 `axiom review status`，修复其中可修复的错误。
9. 不修改漏洞原始正文，不创建 Fact、Todo 或 Intent。

完成所有工具调用后，最后一条 assistant 消息必须只包含 6 个 ASCII 字符：

FINISH

禁止反引号、代码块、句号、解释和总结。
