# AXIOM Explore 收口

停止继续扩展测试，只复用当前会话已经获得的证据。所有 Artifact 内容和漏洞说明必须使用中文。

收口前先确认当前会话已经完整读取 `whatisvul/SKILL.md`；若尚未读取或无法确认，必须运行 `axiom skill path whatisvul` 并完整读取，失败时运行 `axiom skill doctor whatisvul` 排障后重试。必须按该 Skill 逐项复核漏洞成立条件和严重性；未达到标准的内容只能保留为事实或线索，不得上报为漏洞。

1. 整理并检查 `{run_dir}/artifacts/findings.md` 和 `{run_dir}/artifacts/leads.md`，两个文件必须存在且非空；没有内容时写明“本轮无新增确认事实”或“本轮无遗留待验证线索”。
2. 请求、响应、工具输出和漏洞证明只放入 `{run_dir}/artifacts/evidence/`；不要创建 `analysis/`、`reproduction/` 或其它 Artifact 路径，不得注册 `sessions/`、`process/`、`models.json`、凭证和日志。
3. 若已达到漏洞标准，使用 `axiom explore vulnerability report` 逐条补报，并用 `--evidence-path` 关联真实证据文件；不要手动编辑 `explore_result.json`。
4. 读取检查两个核心文件和所有已引用证据，确认路径与内容可用。

完成所有工具调用后，不要描述检查结果或总结执行过程。最后一条 assistant 消息必须只包含 6 个 ASCII 字符：

FINISH

禁止反引号、代码块、句号、解释、总结或其它字符。
