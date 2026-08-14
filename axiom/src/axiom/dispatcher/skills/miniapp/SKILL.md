---
name: miniapp
description: 当测试目标是微信小程序、需要发现/反编译小程序、提取接口与敏感信息时使用
requirements:
  files:
    - scripts/miniapp_decompile_analyze.py
    - scripts/miniapp_regex_rules.json
---

# 微信小程序渗透测试工作流（Miniapp CLI + Reqable MCP）

## 目标

小程序测试不要靠大模型猜接口、猜请求头、猜参数。标准路线是：

1. 用 Miniapp CLI 发现已有解包/分析结果，优先复用。
2. 没有可用结果时，轻量探测未解包包组，让 worker 根据名称、appid、页面标题、域名、业务 code 判断目标。
3. 只用 `target_id` 准备目标，脚本自动定位包路径、解包、分析并生成产物。
4. 读取 `summary.md` 和 `traffic_workflow.md`，再结合 Reqable MCP 的真实流量验证接口。

所有默认状态和产物都写入 `output/miniapp/`，不会因为重启丢失。

## 三步命令

脚本位置：

```bash
python3 "$(axiom skill path miniapp)/scripts/miniapp_decompile_analyze.py" --help
```

不要依赖当前工作目录或猜测仓库路径；`axiom skill path miniapp` 只会解析当前 lease 已启用的 Skill 目录。

### 1. status：先看有没有可直接用的目标

```bash
python3 "$(axiom skill path miniapp)/scripts/miniapp_decompile_analyze.py" status
```

输出并写入：

- `output/miniapp/status.json`
- `output/miniapp/status.md`

重点字段：

- `ready_targets[]`：已经解包/已分析目标，包含 `target_id`、名称、appid、路径、`summary`、`traffic_workflow`。
- `package_targets[]`：未解包包组，只做基础归组，不做深度识别。
- `next_action`：`use_ready_target`、`run_probe` 或 `no_targets`。

如果 `ready_targets` 里有 `is_analyzed=true`，优先直接读返回的 `summary` / `traffic_workflow`，不要重复解包。

### 2. probe：没有可用目标时识别未解包包组

```bash
python3 "$(axiom skill path miniapp)/scripts/miniapp_decompile_analyze.py" probe
```

输出并写入：

- `output/miniapp/probe.json`
- `output/miniapp/probe.md`

`probe` 会按 appid/version 归组，避免把主包和分包分别丢给 worker。重点看：

- `target_id`：下一步唯一要传的 ID。
- `display_name` / `appid` / `page_titles`：判断哪个小程序是目标。
- `hosts` / `business_codes`：判断业务接口线索。
- `developer` / `description`：离线包内通常没有，字段为空时不要当成失败。

### 3. prepare：只传 target_id 生成完整产物

```bash
python3 "$(axiom skill path miniapp)/scripts/miniapp_decompile_analyze.py" prepare --target-id <target_id>
```

worker 不需要拼 `.wxapkg` 路径、版本目录、分包路径或输出目录。脚本会从 `status.json` / `probe.json` 找到目标，自动解包/分析。

输出目录默认是：

```text
output/miniapp/<target_id>/
```

固定产物：

- `decompiled/`：解包目录；失败时保留字符串兜底目录。
- `manifest.json`：appid、名称、版本、来源路径、hash、引擎、是否复用。
- `regex_findings.json`：全部正则命中。
- `api_candidates.json`：path、url、host、接口 code、请求参数候选。
- `sensitive_findings.json`：账号密码、token、key、手机号、身份证、JDBC 等。
- `media_resources.json`：图片、视频、音频、字体等资源。
- `summary.md`：worker 必须先读。
- `traffic_workflow.md`：下一步如何用 Reqable 的真实捕获流量验证接口。

## Worker 固定流程

```bash
python3 "$(axiom skill path miniapp)/scripts/miniapp_decompile_analyze.py" status
```

- 如果返回 `ready_targets` 且 `is_analyzed=true`：读取对应 `summary.md` / `traffic_workflow.md`。
- 如果没有可用 `ready_targets`：运行 `probe`。

```bash
python3 "$(axiom skill path miniapp)/scripts/miniapp_decompile_analyze.py" probe
```

- 根据 `display_name`、appid、页面标题、host、业务 code 判断目标。
- 选中目标后只记录 `target_id`。

```bash
python3 "$(axiom skill path miniapp)/scripts/miniapp_decompile_analyze.py" prepare --target-id <target_id>
```

- 读取输出里的 `summary.md` / `traffic_workflow.md`。
- 使用 Reqable MCP 查询真实 HTTP flow，再基于真实请求包重放和变异。

## Reqable 固定验证流程

Explore Worker 按以下顺序使用 Reqable，不得跳过真实请求恢复环节：

1. `capture_live_filter`：按 Host、Path 或业务 Code 查询候选流量 ID。
2. 没有目标流量时，调用 `capture_live_set_enabled(enabled=true)` 开始捕获，并真实操作小程序触发业务流程。
3. `capture_live_get_by_id(id)`：读取完整 URL、方法、请求头、Cookie、请求体和响应。
4. `capture_live_generate_curl(id)`：从真实记录生成保留原始字段的 cURL。
5. Explore 使用 `bash` 重放 cURL，每次只改变一个身份、对象 ID、状态、数量或业务参数。

`capture_live_compose` 只用于把流量放进 Reqable 调试标签，`capture_live_collection_add` 只用于保存到集合，都不是自动验证必需工具。不要自动调用 `proxy_set` 修改系统代理。

Reason 没有 Reqable Tool 时只读取静态分析结果并规划 Explore Intent，不得声称已经完成真实流量验证。

## 兼容命令

下面命令保留给人工调试或高级场景，不作为 worker 默认流程：

```bash
# 列出候选包/目录
python3 "$(axiom skill path miniapp)/scripts/miniapp_decompile_analyze.py" list --root /path/to/root

# 轻量识别指定根目录
python3 "$(axiom skill path miniapp)/scripts/miniapp_decompile_analyze.py" identify --root /path/to/root --only-unpacked

# 对明确路径完成解包+分析
python3 "$(axiom skill path miniapp)/scripts/miniapp_decompile_analyze.py" all --input /path/to/pkg_or_decompiled_dir --out output/miniapp/<name>

# 只分析已有解包目录
python3 "$(axiom skill path miniapp)/scripts/miniapp_decompile_analyze.py" analyze --input /path/to/decompiled_dir --out output/miniapp/<name>
```

## `.wxapkg` 处理方式

对 `.wxapkg`，脚本会先解析包头和索引表，尽量还原文件目录：

- 已解密包：直接解包到 `decompiled/`。
- 已加密包：如果能从路径/文件名识别 `wx...` appid，会尝试内置解密后解包。
- 解包失败：保留原包到 `raw/`，并生成字符串兜底结果。

如果用户已经有完整反编译目录，`status` 会优先发现并返回可复用目标。

## 注意！！！

- 没有 `summary.md` / `api_candidates.json` 前，不要直接构造业务请求。
- 静态结果只负责给线索，真正测试必须优先找 Reqable 里的真实 HTTP flow。
- 不要只带 Cookie，也不要只带 X-TOKEN；真实请求里的 Cookie、X-TOKEN、Client、Referer、User-Agent、Content-Type、project/version、form 编码都要保持一致。
- 出现“登录失败 / 请先登录 / data 为空 / 太火爆啦 / 参数缺失”等失败请求时，99%的情况是请求头，参数和值不完整导致的，补充好对应部分再尝试。
