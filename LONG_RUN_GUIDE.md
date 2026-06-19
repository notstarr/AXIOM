# 长时间渗透测试运行建议

目标：单个项目连续跑 10+ 小时，多轮 Reason→Explore 循环不中断。

## 1. 超时配置

### 问题

默认 explore 超时 300s，复杂任务跑不完就超时。300s 对于深水区探测定是不够的。

### 建议

在 dispatch 设置页把 explore 超时调到 **600-900s**：

```
bootstrap_timeout: 600
explore_timeout:   600
reason_timeout:    300
```

长超时 = 模型有足够时间在单个 intent 方向深挖。代价是如果模型迷失方向，浪费的时间也更多。加 conclude_timeout 到 120s 让总结更充分。

## 2. 多 Intent 并行

```
reason_max_intents: 4-5
```

每轮 reason 提出 4-5 个 intent，4 个 explore 并行跑。瓶颈是你的 worker 数量——2 个 worker 意味着 2 个并行 + 2 个排队。

如果真的想跑 10 小时不停，建议增加 worker 到 3-4 个（不同模型、或同模型不同配置）。

## 3. Hint 注入——人工纠偏

这是**最重要的机制**。跑了 5 小时后发现模型在兜圈子？不要停任务，直接加 hint：

```
Web UI → 项目详情 → 添加 Hint:
"JWT 密钥看起来是弱密钥，试一下 HS256 none attack"
"放弃 /api/v1，重点看 /api/v2 的新接口"
"config.js 里泄露了内部域名，试试内网横向"
```

Reason 下一轮读图谱时会看到 hints，生成的 intent 会朝这些方向倾斜。**不中断任务就能引导方向**。

## 4. 避免模型疲劳——Worker 轮换

同一个模型连续跑 10 小时会产生疲劳：重复相似的 explore、intent 越来越模糊、输出质量下降。

### 方案 A：定时切换模型（手动）

Web UI 编辑 worker 配置，禁用当前模型、启用另一个。Dispatcher 下次调度时自动换。

### 方案 B：不同阶段的 intent 分给不同 worker

暂时不支持，但可以手动——看到图谱变广了（很多 fact），停掉探索型 worker，启用分析型 worker（更擅长 reason）。

## 5. 防止图谱膨胀——定期"修剪"

跑了 10 小时，fact 可能有 100+ 个。Reason 的 prompt 里塞不下（graph_yaml 太大）。截止点：

- Fact > 50 个 → reason prompt 超长 → token 消耗剧增、质量下降
- Intent > 20 个 → 图谱混乱 → 模型分不清方向

### 缓解

1. 定期把已完成项目 **stop 再 reopen**（清空 open intent，重置状态）
2. 或者手动在 Web UI 删除过时/低价值 fact
3. 或者接受图谱膨胀，增大 reason timeout 让它慢慢读

## 6. 成本控制

10 小时不间断 = 大量 API 调用。以 deepseek/sonnet 的价格：

```
假设每轮 explore 消耗 50K tokens（输入+输出）
每轮 reason 消耗 80K tokens
每小时约 6 轮 reason + 12 个 explore

估算：每小时 ~1.5M tokens
      10 小时 ≈ 15M tokens
```

建议在 API 供应商后台设置 **日额度上限**，防止意外跑飞。

## 7. 自停止条件

目前 reason 判定 goal 达成后会 `complete` 项目。确保 goal 写得足够具体，模型才能判断"达成了"。

不好的 goal：`"对该系统进行渗透测试"`（永远不会 complete，因为没有明确的完成标准）

好的 goal：`"获取该系统的最高权限（root/admin shell），或确认在 5 个主要子系统上均不存在高危漏洞"`

## 8. 监控要点

跑长任务时重点盯这几个指标：

| 指标 | 健康值 | 危险信号 |
|------|--------|----------|
| explore 耗时 | 60-180s | 频繁达到超时（600s） |
| intent 描述重叠 | 无 | 连续 3 轮 intent 方向一样 |
| fact 增长 | 每轮 2+ 个新 fact | 连续 2 轮零增长 |
| vuln 增长 | 持续有新发现 | 超过 5 轮无新 vuln |
| reason no-op | 偶尔 | 连续 3 轮 no-op（无新意图，说明走到头了） |

如果连续 3 轮 reason 返回 no-op 且无新事实，**任务已经到头了**，该停了。

## 9. 推荐配置

```
// dispatch.yaml 或 Web UI
bootstrap_timeout:        600
bootstrap_conclude_timeout: 120
explore_timeout:          600
explore_conclude_timeout: 120
reason_timeout:           300
reason_max_intents:       4
bootstrap_worker_count:   1
explore_worker_count:     3
reason_worker_count:      1
runtime_interval:         5
```

## 10. 一句话总结

长跑的关键不是改代码，而是：**调大超时 + 加 hint 引导 + 定时加/换 worker + 写好 goal 让它知道什么时候该停**。
