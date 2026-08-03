---
name: browser-control
description: 当需要操作浏览器（导航、点击、填表、截图、提取数据）时使用
requirements:
  commands: [bash, curl]
  optional_commands:
    - name: mitmdump
      args: [--version]
      contains: Mitmproxy
  python_modules: [playwright]
  files:
    - scripts/cdp_ctl.py
    - scripts/launch-chrome.sh
    - scripts/stop-chrome.sh
    - scripts/launch-proxy.sh
    - scripts/stop-proxy.sh
    - scripts/proxy_get_traffic.py
    - scripts/mitm_addon.py
  probes:
    - name: shared-chrome-cdp
      command: [python3, "{skill}/scripts/cdp_ctl.py", state]
      contains: "READY:"
      timeout_seconds: 15
---

## 工具概览

本 skill 包含以下脚本（位于 `scripts/` 目录）：

| 脚本 | 作用 |
|------|------|
| `cdp_ctl.py` | Playwright-over-CDP 浏览器控制 CLI（主力工具） |
| `launch-chrome.sh` | 启动 Chrome-SecTest（已登录目标系统的 Chrome 实例） |
| `stop-chrome.sh` | 只停止 Chrome-SecTest，不影响个人 Chrome |
| `launch-proxy.sh` | 幂等启动共享 mitmproxy，不继承 Worker 密钥环境 |
| `stop-proxy.sh` | 只停止 AXIOM 管理的共享 mitmproxy |
| `proxy_get_traffic.py` | 查询 mitmproxy 捕获的 HTTP 流量 |
| `mitm_addon.py` | mitmproxy addon，录制流量到 JSONL |

---

## ⚠️注意⚠️

Runner 会在 Lease 结束时自动关闭由 `cdp_ctl.py new-tab` 注册的 Tab。模型仍应在不再使用时主动关闭，尽早释放页面和 Web Worker：
```bash
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" close-tab {tab_id}
```

## 多 Worker 隔离（必须遵守）

多个 worker 可能同时运行。**每个 worker 必须使用独立的 CDP tab**，通过 `--tab-id` 参数指定：

```bash
# 正确：每个 worker 以 Intent ID 作为稳定别名创建自己的 tab
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {intent_id} new-tab https://target.com
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {intent_id} state
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {intent_id} click 5

# 错误：不加 --tab-id，会操作到其他 worker 的页面
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" state
```

---

## 前置条件

Chrome-SecTest 需要已运行（端口 9223）。启动方式：
```bash
bash "$(axiom skill path browser-control)/scripts/launch-chrome.sh"
```

launch-chrome.sh 是幂等的——已运行则直接返回。多 worker 可各自安全调用。生产无界面运行可由运维设置 `CHROME_HEADLESS=1`；普通 Worker 不应自行切换共享实例的显示模式。

CDP 连接默认最多等待 10 秒，避免共享 Chrome 异常时永久占住 Tool Call。Tab 别名和 Lease 归属记录在 `/tmp/axiom-cdp-tabs-9223.json`；Runner 只清理当前 Lease 注册的 Tab，不会关闭人工打开的页面或其他 Worker 的 Tab。

Chrome 由启动脚本以独立 Session 启动，标准输入/输出/错误均连接到 `/dev/null`，不会继承 Bash Tool Call 的管道。因此即使命令外层使用 `2>&1 | head` 或 `tail`，脚本也会在 CDP 就绪后正常返回，而共享 Chrome-SecTest 会继续运行。非标准安装路径或测试环境可通过 `CHROME_BIN=/path/to/chrome` 覆盖浏览器二进制。

验证是否就绪：
```bash
curl -s http://localhost:9223/json/list | python3 -c "import json,sys; print(f'Chrome-SecTest: {len(json.load(sys.stdin))} tab(s)')" 2>/dev/null || echo "未运行"
```

停止 Chrome-SecTest（安全，不影响个人 Chrome）：
```bash
bash "$(axiom skill path browser-control)/scripts/stop-chrome.sh"
```

---

## cdp_ctl.py 命令手册

### 基本参数

```bash
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" [--cdp URL] [--tab-id ID] [--sleep SECONDS] <command> [args]
```

- `--cdp`：CDP 端点，默认 `http://localhost:9223`
- `--tab-id`：目标 tab 的 CDP target id 或 URL 子串（多 worker 必须指定）
- `--sleep`：点击/导航后等待秒数，默认 1.5

### 页面信息

```bash
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} state              # URL、标题、所有可交互元素（带编号）
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} links              # 所有链接（编号、文字、href）
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} url                # 当前 URL
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} title              # 页面标题
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} text               # 页面可见文字（默认 4000 字符）
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} text --chars 8000  # 更多文字
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} eval "document.cookie"  # 执行 JS
```

### 导航

```bash
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} open "https://target.com"  # 导航并等待加载
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} back                        # 返回上一页
```

### 交互

```bash
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} click 5              # 点击编号 5 的元素
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} click-text "登录"     # 按文字点击
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} click-href "/login"   # 按 href 点击
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} hover 3               # 悬停（触发下拉菜单等）
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} fill 2 "search term"  # 填写输入框
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} select 4 "option"     # 选择下拉框选项
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} keys "Enter"          # 键盘事件（Enter/Escape/Tab）
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} scroll down           # 向下滚动 500px
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} scroll up 1000        # 向上滚动 1000px
```

### 等待

```bash
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} wait text "加载完成"   # 等待文字出现
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} wait url "/dashboard"  # 等待 URL 跳转
```

### 截图

```bash
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} screenshot evidence.png           # 视口截图
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} full-screenshot full_page.png     # 全页截图
```

### Cookies

```bash
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} cookies    # 输出 JSON 格式 cookies
```

### Tab 管理

```bash
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" new-tab "https://target.com"    # 创建新 tab，返回 TAB_ID
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" close-tab {tab_id}               # 关闭 tab
```

---

## 流量捕获（mitmproxy）

如果需要捕获浏览器触发的 HTTP 流量（爬虫场景常用）：

### 启动 mitmproxy

只允许使用受管启动器，不得直接执行 `mitmdump ... &`。受管进程不会继承模型 API Key、Lease 等 Worker 环境，并使用 PID 文件避免遗留多个后台实例：
```bash
bash "$(axiom skill path browser-control)/scripts/launch-proxy.sh"
```

Chrome-SecTest 需要以代理模式启动：
```bash
MITM_PROXY=1 bash "$(axiom skill path browser-control)/scripts/launch-chrome.sh"
```

`launch-chrome.sh` 在 MITM 模式下会自动确认共享代理已启动。默认日志每 64 MiB 轮转，保留 3 个分片；可由运维通过 `PROXY_FLOW_MAX_BYTES`、`PROXY_FLOW_BACKUPS` 覆盖。停止共享代理：

```bash
bash "$(axiom skill path browser-control)/scripts/stop-proxy.sh"
```

### 查询流量

```bash
# 操作前清空
python3 "$(axiom skill path browser-control)/scripts/proxy_get_traffic.py" --clear

# 操作后查看触发的 API
python3 "$(axiom skill path browser-control)/scripts/proxy_get_traffic.py" --type api --summary

# 查看特定路径的流量
python3 "$(axiom skill path browser-control)/scripts/proxy_get_traffic.py" --filter /api/ --type api --summary

# 查看完整请求/响应
python3 "$(axiom skill path browser-control)/scripts/proxy_get_traffic.py" --filter /api/v1/user --full --last 1

# 查看状态
python3 "$(axiom skill path browser-control)/scripts/proxy_get_traffic.py" --status
```

---

## 标准操作流程

### 打开页面并获取元素

```bash
# 1. 创建 tab（首次）
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" new-tab "https://target.com"

# 2. 查看元素编号
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {tab_id} state

# 3. 截图留证
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {tab_id} screenshot before.png

# 4. 点击目标元素
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {tab_id} click 5

# 5. 确认操作结果
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {tab_id} state
```

### 填写表单并提交

```bash
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} open "https://target.com/login"
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} state                    # 查看输入框编号
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} fill 1 "user@test.com"   # 填写用户名
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} fill 2 "password123"     # 填写密码
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} click 3                   # 点击登录
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} wait text "欢迎"          # 等待登录成功
```

### 操作后查看触发的 API

```bash
python3 "$(axiom skill path browser-control)/scripts/proxy_get_traffic.py" --clear                      # 清空旧流量
python3 "$(axiom skill path browser-control)/scripts/cdp_ctl.py" --tab-id {id} click 5                   # 执行操作
python3 "$(axiom skill path browser-control)/scripts/proxy_get_traffic.py" --type api --summary          # 查看触发的 API
```

---

## 常见问题

| 问题 | 解决 |
|------|------|
| `CDP_ERROR: connect failed` | Chrome-SecTest 未运行，执行 `bash "$(axiom skill path browser-control)/scripts/launch-chrome.sh"` |
| `connect_over_cdp` 超时 | 先确认没有活动浏览器 Lease；随后停止并重启 Chrome-SecTest。新 Lease 的 Tab 会自动回收，避免再次长期堆积 |
| 启动提示 `Port 9223 is occupied by a non-SecTest Chrome instance` | 9223 端口被你的个人 Chrome 占用。脚本会拒绝接管，避免误关你的页面。请改用其他 `CDP_PORT` 或释放 9223 |
| 流量文件很大 | 查询器只反向读取最近记录，不会全量加载；新流量文件会自动轮转 |
| 抢页面/被其他 worker 导航 | 确认每次都带了 `--tab-id {你的ID}` |
| click 报 index out of range | 先执行 `state` 获取最新元素编号 |
| 页面没加载完就操作 | 用 `wait text "xxx"` 或 `wait url "xxx"` 等待 |
| 截图空白 | 用 `full-screenshot` 替代 `screenshot`，或加 `wait` |
| `scroll` 没效果 | 页面可能用了虚拟滚动，用 `eval "window.scrollTo(0, 99999)"` |

---

## 安全约束（避免误关个人浏览器）

- `cdp_ctl.py` 只做 CDP 连接/断开，不应关闭整个浏览器进程。
- `launch-chrome.sh` 只会复用带 `Chrome-SecTest` user-data-dir 的实例。
- 若 9223 被非 SecTest Chrome 占用，会直接报错退出（fail-fast），不会附着到你的个人浏览器。
