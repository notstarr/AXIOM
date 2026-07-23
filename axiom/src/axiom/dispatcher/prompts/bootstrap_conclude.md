# AXIOM Bootstrap 收口

任务 `{task_id}`、Intent `{intent_id}` 的 Bootstrap 执行已经到达收口阶段。

只根据已有运行目录 `{run_dir}` 和已经收集的证据整理产物，不进行新的网络探索、目录搜索或工具调用。所有解释性文字必须使用中文。

必须补齐并覆盖写入：

`{run_dir}/bootstrap_artifacts.json`

该 JSON 必须包含非空字段 `asset_inventory`、`initial_leads`、`findings`。不要为了满足格式虚构资产、线索或漏洞；没有确认漏洞时在 `findings` 明确说明本阶段没有确认漏洞。不要创建 Intent/Todo、不要修改数据库、不要复制敏感凭证。
