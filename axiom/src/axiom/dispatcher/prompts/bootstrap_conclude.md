# AXIOM 引导阶段收口（Bootstrap Conclude）

你承接任务 `{task_id}`、执行意图（Intent）`{intent_id}` 已进入收口。收口只整理当前会话已经获得的证据，不进行新的网络探索、目录搜索或网络探测型工具调用；只允许读取技能（Skill）与已有文件，并补齐阶段产物。

收口阶段没有 Bash，不得重新运行 Skill CLI。直接复用执行阶段已经读取的 `whatisvul/SKILL.md` 和当前会话记忆；如果执行阶段未成功读取该 Skill，则如实整理已有材料，不要在收口阶段重新探测。必须用已经读取的漏洞标准清理 `findings.md`：证据或实际影响不足的内容应转入 `initial_leads.md`，不得凭印象保留为漏洞。

只允许在 `{run_dir}` 补齐并覆盖写入以下三个非空中文 Markdown 文件：

- `{run_dir}/asset_inventory.md`
- `{run_dir}/initial_leads.md`
- `{run_dir}/findings.md`

整理时保持 **API 能力树** 结构，禁止把已展开的拼装函数/子路径压扁成“看见的 URL 列表”。缺前缀域、缺参数来源、缺鉴权矩阵、三类入口（菜单可见 / 代码可达 / 无登录）缺一类时，在 `asset_inventory.md` 写 `JS 资产收集状态：部分完成` 和具体阻塞，不要假装覆盖完整。同一底座的读写触发缺口在 `initial_leads.md` 里保持一条线索，不要拆散。

不要创建 JSON，不要修改数据库，不要创建执行意图（Intent）或待办（Todo），不要复制凭证；没有确认漏洞时，在 `findings.md` 明确写出本阶段没有确认漏洞。完成后读取检查三个文件。

确认三个文件均已写完且可读取后，最后一条助手消息必须严格等于：

FINISH

禁止反引号、代码块、句号、解释、总结或其它字符。
