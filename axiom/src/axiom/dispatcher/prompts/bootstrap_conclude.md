# AXIOM Bootstrap 收口

任务 `{task_id}`、Intent `{intent_id}` 已进入收口。收口只整理当前会话已经获得的证据，不进行新的网络探索、目录搜索或工具调用。

只允许在 `{run_dir}` 补齐并覆盖写入以下三个非空中文 Markdown 文件：

- `{run_dir}/asset_inventory.md`
- `{run_dir}/initial_leads.md`
- `{run_dir}/findings.md`

不要创建 JSON，不要修改数据库，不要创建 Intent/Todo，不要复制凭证；没有确认漏洞时，在 `findings.md` 明确写出本阶段没有确认漏洞。完成后读取检查三个文件。

最后一条 assistant 文本必须严格等于 `FINISH`，不得附带任何说明。
