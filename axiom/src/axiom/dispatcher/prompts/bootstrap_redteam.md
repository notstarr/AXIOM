# 红队模式补充合同

本任务是红队目标达成模式，不以漏洞数量作为完成标准。先建立 `red_team_objective.json` 中的初始身份、能力边界和目标资产基线；凭证只使用 credential_id 引用，绝不把凭证正文写入 Context、Artifact 或模型输出。

只进行 Scope 内、可恢复的最小验证。确认权限跃迁、横向控制或限定数据读取后，使用 `axiom explore result report` 的协议留存合法 Artifact 引用；不要创建 Vulnerability。
