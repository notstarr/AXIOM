# 红队引导收口

Task：`{task_id}` | Intent：`{intent_id}`。停止网络探测，只整理已有材料。

在 `{run_dir}` 补齐两个非空中文 Markdown：`asset_inventory.md`、`initial_leads.md`。`asset_inventory.md` 仍按 Web 应用、后端域名 / API、主机与服务、其它四类登记已经拿到的资产，并在开头写明盲区。没有可交给后续 worker 的入口时，`initial_leads.md` 写“本阶段未取得可上报成果”。

写完后最后一条助手消息只能是：

FINISH
