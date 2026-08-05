# AXIOM Reason Provider 错误分类与 Worker 冷却

## 问题现象

生产 `rr000435` 的前几次 Reason Lease 被记录为 `reason_plan_invalid`，表面看像模型忘记调用
`goal assess` 或只输出了错误 FINISH。检查 Pi JSON stdout 后确认，模型实际上没有产生有效 assistant 文本
或工具调用：Provider 持续返回 `429 Token Plan 用量上限`。

Pi 在自动重试耗尽后输出了 assistant `stopReason=error` 和 `auto_retry_end success=false`，但外层进程退出码
仍为 0。旧 Adapter 只检查退出码，于是继续提取空 assistant 文本；ReasonRunner 随后把它当作 FINISH/
Workspace 不合规，进入三次 Conclude，最终错误归类为业务规划失败。一次 Provider 故障因此还会被额外的
收口请求放大，并消耗执行重试额度。

LongCat 后续 Lease 能正常读取 Context、调用 `axiom reason goal assess` 并创建 `i064`，进一步证明本次
根因在运行时错误识别，而不是 Reason Prompt 或 Workspace 合同。

## 修复

### Pi JSON 终态识别

Pi Adapter 现在逐行解析 JSON stdout，并只判断最终状态：

- 最后一个 assistant `stopReason=error` 时读取 `errorMessage`；
- `auto_retry_end success=false` 时读取 `finalError`；
- 后续 assistant 成功或 `auto_retry_end success=true` 会清除先前错误，避免把已恢复的短暂失败误报；
- 终态 Provider 错误在 assistant 文本提取和 FINISH 校验之前抛出 `TransientWorkerError`；
- 返回码非零和 `axiom_tool_load_failed` 的原有处理保持不变。

普通的“退出码 0 + 空文本”如果没有 Provider 终态错误，仍进入原 FINISH/Workspace 校验，不会被一概当成
基础设施故障。

### Reason 失败路径

ReasonRunner 已有 `TransientWorkerError` 分支，现在 Provider 终态错误会直接走该路径：

```text
failure_kind  = worker_infrastructure
failure_class = transient
```

该路径会废弃当前 Workspace、释放 Lease，并且：

- 不进入 Reason Conclude；
- 只增加 `infrastructure_retry_count`；
- 不增加 `retry_count`；
- ReasonRun 回到 pending，等待其它健康后端或冷却结束。

### Worker 冷却

`ReasonService.release_run()` 现在与 Explore/Reviewer 的 transient 处理一致：如果失败 Lease 有 Worker，按
现有 `worker_failure_cooldown_seconds` 写入 `workers.cooldown_until`。调度器自然优先选择其它可用 Reason
后端；没有可用后端时工作保持 pending，不新增 429 专用状态、配置或调度器分支。

## 边界

- 不修改 Reason Prompt、FINISH、Workspace readiness、收口次数或阶段预算。
- 不新增数据库字段、迁移或 HTTP API。
- 只依据最终 JSON 状态分类；早期错误后成功不会触发 transient release。
- Explore RPC 既有 Provider 错误处理保持原样。

## 验证

- JSON 退出码为 0、最终 assistant 为 429 时抛出 `TransientWorkerError`；
- 历史 503 后最终成功仍返回 `FINISH`；
- 无 Provider 错误的空文本继续走阶段格式校验；
- Reason Provider 错误不会调用 Conclude；
- Workspace 标记为 `abandoned/worker_infrastructure`；
- `infrastructure_retry_count + 1`、`retry_count` 不变；
- 故障 Worker 写入 `cooldown_until`。

本轮全量验证为 `480 passed, 1 warning`；唯一 warning 是既有 Starlette/httpx 弃用提示。
