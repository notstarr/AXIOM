# AXIOM Explore 收口

停止继续扩展测试，只复用当前会话已经获得的证据。所有 Artifact 内容和漏洞说明必须使用中文。

1. 整理并检查 `{run_dir}/artifacts/findings.md` 和 `{run_dir}/artifacts/leads.md`，两个文件必须存在且非空；没有内容时写明“本轮无新增确认事实”或“本轮无遗留待验证线索”。
2. 请求、响应、工具输出和漏洞证明只放入 `{run_dir}/artifacts/evidence/`；不要创建 `analysis/`、`reproduction/` 或其它 Artifact 路径，不得注册 `sessions/`、`process/`、`models.json`、凭证和日志。
3. 若已达到漏洞标准，使用 `axiom explore vulnerability report` 逐条补报，并用 `--evidence-path` 关联真实证据文件；不要手动编辑 `explore_result.json`。
4. 读取检查两个核心文件和所有已引用证据，确认路径与内容可用。

最后一条 assistant 文本必须严格等于 `FINISH`，不得附带任何说明。
