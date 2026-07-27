# AXIOM Explore 收口

停止继续扩展测试，只复用当前会话已经获得的证据。所有 Artifact 内容和漏洞说明必须使用中文。

1. 在 `{run_dir}/artifacts/` 整理至少一个非空、可审计的证据文件；不得扫描或注册 `sessions/`、`process/`、`models.json`、凭证和日志。
2. 若已达到漏洞标准，使用 `axiom explore vulnerability report` 逐条补报；不要手动编辑 `explore_result.json`。
3. 读取检查 Artifact 文件，确认路径和内容可用。

最后一条 assistant 文本必须严格等于 `FINISH`，不得附带任何说明。
