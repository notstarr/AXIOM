# AXIOM 引导阶段收口（Bootstrap Conclude）

Task：`{task_id}` | Intent：`{intent_id}`。收口阶段没有 Bash，**禁止网络探测**，复用执行阶段已经读取的 `whatisvul/SKILL.md` 与已有材料整理产物。

1. 在 `{run_dir}` 补齐并覆盖以下三个非空中文 Markdown：
   - `asset_inventory.md`（API 能力树、参数槽与 ID 来源、鉴权矩阵、收集状态）；
   - `initial_leads.md`（高价值未确认线索）；
   - `findings.md`（确认漏洞，无则写“本阶段未确认漏洞”）。
2. 保持能力树结构，禁止将子路径压扁为简单 URL 列表。
3. **覆盖自检（必须做）**：确认能力树已合并 `{run_dir}` 下**全部** `jsfinder_result*.json`（每轮/每入口一份，不是只看最后一份）。若执行阶段发现过新的前端路由或入口（其它路径、管理台、其它子域）却没有对应的补跑产物，**不得收口**，先在报告中如实记为「收集不全：<入口> 未扫」。

确认三个文件写完后，最后一条助手消息必须严格只有：

FINISH

禁止反引号、代码块、句号、解释或总结。
