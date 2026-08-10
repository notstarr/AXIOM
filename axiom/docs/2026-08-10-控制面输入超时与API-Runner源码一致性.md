# AXIOM 控制面输入超时与 API／Runner 源码一致性

## 背景

2026 年 8 月 10 日排查任务 `t_163` 的 Bootstrap 失败，以及随后 Reason 反复释放重试的问题。结论是两类运行时问题叠加：

1. 控制面 `input` 请求的默认 HTTP 超时过短，Context、运行目录和能力快照构建稍慢时，Runner 在真正启动 Pi 之前就拿不到 `ExecutionInput`。
2. API 与 Runner 没有同时重启时，可能出现旧进程产生的 `ExecutionInput` 与新版本 Pi Adapter 混用，暴露出 `reason_prompt_profile` 缺失的兼容错误。

这不是 Reason 提示词忘记调用工具，也不是目标任务本身导致的执行失败。

## t_163 的实际过程

时间为中国标准时间（CST，UTC+8）：

| 时间 | 阶段 | 结果 |
|---|---|---|
| 2026-08-10 14:02:46 | Bootstrap 第一次尝试 | `POST /api/v1/dispatch/tasks/t_163/intents/i001/input` 超时 |
| 2026-08-10 14:03:37 | Bootstrap 第二次尝试 | 同一 `input` 控制面请求再次超时 |
| 2026-08-10 14:05:00 | Bootstrap 第三次尝试 | 成功领取并启动 |
| 2026-08-10 14:09:44 | Bootstrap | 正常完成并写入 Fact |

前两次失败都发生在控制面输入阶段，Pi 模型没有启动、没有读取 Context，也没有执行 Bootstrap 工具。旧逻辑将它们记录成 `bootstrap_execution_failed` / `execution`，导致看起来像 Bootstrap 自身执行失败，并消耗了普通执行重试额度。

第三次 Bootstrap 成功后写入：

```text
f001
artifact:a001  # 资产清单
artifact:a002  # 初始线索
artifact:a003  # 已确认问题
```

当前图谱中 `i001` 已完成，`retry_count=2` 是前两次失败留下的历史执行重试计数；这不表示第三次执行仍然失败。

## rr000509 的版本错配

Reason `rr000509` 曾出现：

```text
'ExecutionInput' object has no attribute 'reason_prompt_profile'
```

原因是 API 和 Runner 的启动时间早于新版源码，两个进程没有同时更新，导致旧的输入对象与新版 Adapter 组合运行。修改 `ExecutionInput`、Pi Adapter、Prompt 注入或执行协议后，必须同时重启 API 和 Runner；不能只重启其中一个。

## 修复内容

### 1. 延长控制面 input 超时

仅对控制面 `input` 请求使用 120 秒超时，其他请求继续使用原有默认超时。这样可以覆盖 Context、运行目录和能力快照准备时间，但不会放宽单个模型执行预算。

### 2. 明确区分控制面瞬态故障

以下情况归类为可重试的瞬态控制面故障：

- 连接失败或 HTTP 客户端异常；
- `408 Request Timeout`；
- `429 Too Many Requests`；
- `5xx` 服务端错误。

释放时记录：

```text
failure_kind=control_plane_unavailable
failure_class=transient
```

### 3. 不再让控制面错误进入 Conclude

Bootstrap、Reason、Explore 和 Reviewer 在获得 `ExecutionInput` 前遇到控制面瞬态错误时，直接释放当前 Lease：

- 不启动模型执行；
- 不调用 Conclude；
- 不把空输入误判为 `FINISH` 或 Workspace 不合规；
- 只增加 `infrastructure_retry_count`；
- 不增加普通 `retry_count`；
- Reason 继续沿用现有 Worker 冷却逻辑。

没有可用后端时，任务保持 pending，等待其他后端或冷却结束。

### 4. 旧输入对象兼容

Reason Prompt profile 由控制面按 `ReasonRun.trigger` 固定注入。Pi Adapter 对缺少 `reason_prompt_profile` 的旧 `ExecutionInput` 回退到 `default`，避免 API/Runner 短暂错配时直接抛出属性错误。该兼容逻辑不能替代双进程同步重启。

## 当前运行基线

2026-08-10 验证：

```text
API PID    30892
Runner PID 30895
源码       /Users/hexin/vscode/xrayAI/axiom
API        http://127.0.0.1:8888
数据库     PostgreSQL
迁移       20260806_pg0002
```

API 与 Runner 均从同一源码目录启动，健康检查正常。8890 端口上的旧 cairn 服务是独立链路，不属于当前 AXIOM 8888 API／Runner。

## 运维规则

每次发布涉及以下任一内容时，必须同时重启 API 和 Runner：

- `ExecutionInput` 或其他跨 API／Runner 的 Pydantic Schema；
- Pi Adapter、JSON/RPC 执行协议；
- 阶段 Prompt profile 注入；
- Lease、Context 或能力快照字段。

重启后至少检查：

```bash
ps -p <api_pid>,<runner_pid> -o pid,lstart,command
curl -fsS http://127.0.0.1:8888/health
```

确认两个进程的启动时间接近、源码路径一致，并确认健康检查中的数据库和迁移版本符合预期。

## 验证记录

已完成：

- 控制面 `input` 使用 120 秒超时的回归测试；
- Bootstrap 控制面瞬态错误不进入执行和 Conclude 的测试；
- Reason 控制面瞬态错误直接释放的测试；
- Ruff、`compileall` 和 `git diff --check`；
- 相关测试集重新执行：77 项通过，1 项既有的异步启动时序测试失败：
  `tests/test_bootstrap_runner.py::test_axiom_runner_cancels_local_execution_when_task_stops`。

最后一项与本次控制面错误分类修复无关，表现为测试在 2 秒窗口内未观察到阻塞执行器的 started 信号，需单独处理测试时序后再恢复全量绿灯。
