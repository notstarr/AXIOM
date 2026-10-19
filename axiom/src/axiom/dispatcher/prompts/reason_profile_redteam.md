# 红队 Reason 场景策略

本轮按积分缺口选链，不按覆盖缺口派发。先读本轮新 Fact 的 findings.md 和 leads.md，再决定链的状态和下一条 Intent。

一条链是一条能涨分的路径。Intent 是这条链上的一步。没有在途 Intent 的 open 或 partial 链要继续派下一步，或改成 blocked、completed、abandoned。新链只在它能换到更高积分时才开。涨分按 `redteam-scoring`：优先靶标、域控、核心服务器最高权限、核心数据大档和穿透。Web 管理员、办公账号和边缘资产分低，不为它们单开覆盖。
