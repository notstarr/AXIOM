# 红队 Explore 补充合同

执行当前 Chain 的单步行动，保存 findings.md、leads.md 和必要证据。成果按 `permission`、`data`、`credential`、`pivot` 单独上报；数据只填样本数、字节数、允许字段、哈希和脱敏摘要，凭证只填引用 ID/哈希，禁止输出明文。

上报时使用当前 Intent 绑定的 Chain：`axiom explore result report` 可省略 `--chain-id`，控制面已通过环境变量 `AXIOM_CHAIN_ID` 注入本 Intent 关联的 Chain；不要上报到其它 Chain。

**上报前必读 `redteam-scoring` skill**（`axiom skill path redteam-scoring`）：每条成果必须按评分表**自评申报分**（`--points`，1-50），证据只够低档就报低档；同一能力已被 verified 过的不重复上报。
