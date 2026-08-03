---
tags: [axiom, pi, bootstrap, runner, runtime]
status: current-verified
updated: 2026-08-03
---

# Pi 运行时预检与 Bootstrap 重试修复

## 现象

`t_154/i001` 在 Bootstrap 领取后反复出现：

```text
claimed
→ runner_dispatch_failed
→ released / retry_wait
→ 再次 claimed
```

Intent 本身尚未进入 Bootstrap Prompt 或工具执行，`retry_count=0`，但 `infrastructure_retry_count` 连续增加。

## 根因

Runner 在创建阶段 Executor 前调用 `validate_pi_worker_runtime()`。旧实现通过以下命令检查固定 Pi 版本：

```text
runtime/pi/0.82.1/bin/pi --version
```

Pi 0.82.x 的 `--version` 仍会经过完整 CLI 启动路径，可能加载扩展、配置和模型目录。该检查的硬超时只有 5 秒；一旦冷启动或资源竞争超过 5 秒，Runner 就把健康的固定运行时误判为不可用，并把刚领取的 Lease 以 `runner_dispatch_failed` 释放。

异常不会进入 `lru_cache`，所以每次重新领取都会再次执行同一个可能超时的预检，形成“领取—释放—等待重试”循环。

## 修复

- 固定 Pi 路径校验保持不变，仍拒绝 `/usr/local/bin/pi` 或其它未固定二进制。
- Pi 版本改为读取固定运行目录中的本地包元数据：

  ```text
  runtime/pi/0.82.1/node_modules/@earendil-works/pi-coding-agent/package.json
  ```

- 正常生产路径不再启动 Pi CLI，也不触发网络、扩展或模型目录初始化。
- Node 仍通过固定绝对路径执行 `--version`，该操作是轻量且确定性的。
- 不保留 `pi --version` 或 `--offline` 回退；包元数据是固定生产运行时的强制校验来源。校验同时确认包名、版本、`bin.pi` 和 CLI 入口位于包目录内且可执行，避免通过伪造版本脚本绕过检查。
- 成功校验继续由现有 `lru_cache` 在 Runner 进程内复用。

这项修改不改变 Bootstrap、Reason、Explore 的执行预算、Lease、重试、Prompt 或 Pi 模式，只消除阶段启动前的假基础设施失败。

## 回归测试

新增测试构造一个“只要被执行就会写 marker 并睡眠”的假 Pi 启动器，同时提供正确的本地 package metadata。运行时校验必须成功且 marker 不得出现，从而证明生产预检不会再启动 Pi CLI；另覆盖错误包名、版本、`bin.pi`、越界和缺失入口。

同时保留：

- 固定路径检查；
- Pi 版本错误检查；
- Node 版本错误检查；
- Runner 在阶段启动前拒绝未固定 Pi 路径。

生产恢复后，`t_154/i001` 应在下一次领取时直接进入 Bootstrap 活动和心跳，不再新增 `runner_dispatch_failed`。

本次代码和测试验证尚未替代生产验证：此前 `t_154/i001` 第五次偶然成功发生在仍运行旧代码的 Runner（PID 79766）中，不能作为本修复已上线的证据。应用尚未重启，待重启后再用新 Lease 验证 Bootstrap 直接进入活动和心跳。
