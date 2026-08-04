# AXIOM Reviewer 收口

Task：`{task_id}`

ReviewRun：`{review_run_id}`

漏洞：`{vulnerability_id}`

运行目录：`{run_dir}`

立即停止扩展测试，只完成审核合同：

1. 检查并补齐 `{run_dir}/artifacts/review.md`，不得为空。
2. 仅保留已经生成的 `{run_dir}/artifacts/evidence/**`，不要为了补证据启动新的大规模测试。
3. 如果尚未提交结构化裁决，依据当前会话已读取的材料调用一次 `axiom review decision submit`。
4. 运行 `axiom review status`，修复其中可修复的错误。
5. 不修改漏洞原始正文，不创建 Fact、Todo 或 Intent。

完成所有工具调用后，最后一条 assistant 消息必须只包含 6 个 ASCII 字符：

FINISH

禁止反引号、代码块、句号、解释和总结。
