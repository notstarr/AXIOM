# 红队 Explore 收口

停止网络探测，只整理已有材料。中文输出。

- Task：`{task_id}` | Intent：`{intent_id}`
- 任务书：`{intent_description}`
- 运行目录：`{run_dir}`

1. `{run_dir}/artifacts/findings.md` 和 `leads.md` 必须存在且非空。没有新事实就写“本轮无新增确认事实”；没有遗留方向就写“本轮无遗留方向”。措辞高效精炼，零冗余。
2. 已确认且按 `redteam-scoring` 可计分的成果用 `axiom explore result report` 补报，`--points` 按单价 × 数量，`--target-asset` 写实际打进去的公网入口（如 `api.kkgroup.cn`、`119.23.232.213:8080/prismart`；落点在内网时也先写公网入口，内网 IP 放括号里），证据用 `--evidence-path evidence/<file>`。成果记到当前 Intent 已绑定的链。
3. 必须人类完成的物理动作才调用 `axiom explore human-intervention mark`。

最后一条助手消息只能是：

FINISH
