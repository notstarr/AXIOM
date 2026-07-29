# AXIOM 引导阶段收口（Bootstrap Conclude）

任务 `{task_id}`、执行意图（Intent）`{intent_id}` 已进入收口。收口只整理当前会话已经获得的证据，不进行新的网络探索、目录搜索或网络探测型工具调用；只允许读取技能（Skill）与已有文件，并补齐阶段产物。

收口前先确认当前会话已经完整读取 `whatisvul/SKILL.md`；如果尚未读取或无法确认，必须运行 `axiom skill path whatisvul` 并完整读取该文件，失败时运行 `axiom skill doctor whatisvul` 排障后重试。必须用其中的漏洞标准清理 `findings.md`：证据或实际影响不足的内容应转入 `initial_leads.md`，不得凭印象保留为漏洞。

只允许在 `{run_dir}` 补齐并覆盖写入以下三个非空中文 Markdown 文件：

- `{run_dir}/asset_inventory.md`
- `{run_dir}/initial_leads.md`
- `{run_dir}/findings.md`

不要创建 JSON，不要修改数据库，不要创建执行意图（Intent）或待办（Todo），不要复制凭证；没有确认漏洞时，在 `findings.md` 明确写出本阶段没有确认漏洞。完成后读取检查三个文件。

最后一条助手消息必须严格等于 `FINISH`，不得附带任何说明。
