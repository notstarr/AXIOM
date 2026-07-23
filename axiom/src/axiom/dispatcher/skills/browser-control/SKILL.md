---
name: browser-control
description: 当需要操作浏览器（导航、点击、填表、截图、提取数据）时使用
---

## 工具概览

本 skill 包含以下脚本（位于 `scripts/` 目录）：

| 脚本 | 作用 |
|------|------|
| `cdp_ctl.py` | Playwright-over-CDP 浏览器控制 CLI（主力工具） |
| `launch-chrome.sh` | 启动 Chrome-SecTest（已登录目标系统的 Chrome 实例） |
| `stop-chrome.sh` | 只停止 Chrome-SecTest，不影响个人 Chrome |
| `proxy_get_traffic.py` | 查询 mitmproxy 捕获的 HTTP 流量 |
| `mitm_addon.py` | mitmproxy addon，录制流量到 JSONL |

---

## ⚠️注意⚠️

**在任务完成后必须关闭 tab，否则会导致系统卡顿：**
```bash
python3 scripts/cdp_ctl.py close-tab {tab_id}
```

## 多 Worker 隔离（必须遵守）

多个 worker 可能同时运行。**每个 worker 必须使用独立的 CDP tab**，通过 `--tab-id` 参数指定：

```bash
# 正确：每个 worker 创建自己的 tab，互不干扰
python3 scripts/cdp_ctl.py --tab-id {intent_id} new-tab https://target.com
python3 scripts/cdp_ctl.py --tab-id {intent_id} state
python3 scripts/cdp_ctl.py --tab-id {intent_id} click 5

# 错误：不加 --tab-id，会操作到其他 worker 的页面
python3 scripts/cdp_ctl.py state
```

---

## 前置条件

Chrome-SecTest 需要已运行（端口 9223）。启动方式：
```bash
bash scripts/launch-chrome.sh
```

launch-chrome.sh 是幂等的——已运行则直接返回。多 worker 可各自安全调用。

验证是否就绪：
```bash
curl -s http://localhost:9223/json/list | python3 -c "import json,sys; print(f'Chrome-SecTest: {len(json.load(sys.stdin))} tab(s)')" 2>/dev/null || echo "未运行"
```

停止 Chrome-SecTest（安全，不影响个人 Chrome）：
```bash
bash scripts/stop-chrome.sh
```

---

## cdp_ctl.py 命令手册

### 基本参数

```bash
python3 scripts/cdp_ctl.py [--cdp URL] [--tab-id ID] [--sleep SECONDS] <command> [args]
```

- `--cdp`：CDP 端点，默认 `http://localhost:9223`
- `--tab-id`：目标 tab 的 CDP target id 或 URL 子串（多 worker 必须指定）
- `--sleep`：点击/导航后等待秒数，默认 1.5

### 页面信息

```bash
python3 scripts/cdp_ctl.py --tab-id {id} state              # URL、标题、所有可交互元素（带编号）
python3 scripts/cdp_ctl.py --tab-id {id} links              # 所有链接（编号、文字、href）
python3 scripts/cdp_ctl.py --tab-id {id} url                # 当前 URL
python3 scripts/cdp_ctl.py --tab-id {id} title              # 页面标题
python3 scripts/cdp_ctl.py --tab-id {id} text               # 页面可见文字（默认 4000 字符）
python3 scripts/cdp_ctl.py --tab-id {id} text --chars 8000  # 更多文字
python3 scripts/cdp_ctl.py --tab-id {id} eval "document.cookie"  # 执行 JS
```

### 导航

```bash
python3 scripts/cdp_ctl.py --tab-id {id} open "https://target.com"  # 导航并等待加载
python3 scripts/cdp_ctl.py --tab-id {id} back                        # 返回上一页
```

### 交互

```bash
python3 scripts/cdp_ctl.py --tab-id {id} click 5              # 点击编号 5 的元素
python3 scripts/cdp_ctl.py --tab-id {id} click-text "登录"     # 按文字点击
python3 scripts/cdp_ctl.py --tab-id {id} click-href "/login"   # 按 href 点击
python3 scripts/cdp_ctl.py --tab-id {id} hover 3               # 悬停（触发下拉菜单等）
python3 scripts/cdp_ctl.py --tab-id {id} fill 2 "search term"  # 填写输入框
python3 scripts/cdp_ctl.py --tab-id {id} select 4 "option"     # 选择下拉框选项
python3 scripts/cdp_ctl.py --tab-id {id} keys "Enter"          # 键盘事件（Enter/Escape/Tab）
python3 scripts/cdp_ctl.py --tab-id {id} scroll down           # 向下滚动 500px
python3 scripts/cdp_ctl.py --tab-id {id} scroll up 1000        # 向上滚动 1000px
```

### 等待

```bash
python3 scripts/cdp_ctl.py --tab-id {id} wait text "加载完成"   # 等待文字出现
python3 scripts/cdp_ctl.py --tab-id {id} wait url "/dashboard"  # 等待 URL 跳转
```

### 截图

```bash
python3 scripts/cdp_ctl.py --tab-id {id} screenshot evidence.png           # 视口截图
python3 scripts/cdp_ctl.py --tab-id {id} full-screenshot full_page.png     # 全页截图
```

### Cookies

```bash
python3 scripts/cdp_ctl.py --tab-id {id} cookies    # 输出 JSON 格式 cookies
```

### Tab 管理

```bash
python3 scripts/cdp_ctl.py new-tab "https://target.com"    # 创建新 tab，返回 TAB_ID
python3 scripts/cdp_ctl.py close-tab {tab_id}               # 关闭 tab
```

---

## 流量捕获（mitmproxy）

如果需要捕获浏览器触发的 HTTP 流量（爬虫场景常用）：

### 启动 mitmproxy

mitmproxy 通常由系统自动拉起。手动启动：
```bash
PROXY_FLOW_FILE=/tmp/proxy_flows.jsonl \
MITM_TARGET_HOST=target.example.com \
mitmdump -s scripts/mitm_addon.py --listen-port 18080 -q &
```

Chrome-SecTest 需要以代理模式启动：
```bash
MITM_PROXY=1 bash scripts/launch-chrome.sh
```

### 查询流量

```bash
# 操作前清空
python3 scripts/proxy_get_traffic.py --clear

# 操作后查看触发的 API
python3 scripts/proxy_get_traffic.py --type api --summary

# 查看特定路径的流量
python3 scripts/proxy_get_traffic.py --filter /api/ --type api --summary

# 查看完整请求/响应
python3 scripts/proxy_get_traffic.py --filter /api/v1/user --full --last 1

# 查看状态
python3 scripts/proxy_get_traffic.py --status
```

---

## 标准操作流程

### 打开页面并获取元素

```bash
# 1. 创建 tab（首次）
python3 scripts/cdp_ctl.py new-tab "https://target.com"

# 2. 查看元素编号
python3 scripts/cdp_ctl.py --tab-id {tab_id} state

# 3. 截图留证
python3 scripts/cdp_ctl.py --tab-id {tab_id} screenshot before.png

# 4. 点击目标元素
python3 scripts/cdp_ctl.py --tab-id {tab_id} click 5

# 5. 确认操作结果
python3 scripts/cdp_ctl.py --tab-id {tab_id} state
```

### 填写表单并提交

```bash
python3 scripts/cdp_ctl.py --tab-id {id} open "https://target.com/login"
python3 scripts/cdp_ctl.py --tab-id {id} state                    # 查看输入框编号
python3 scripts/cdp_ctl.py --tab-id {id} fill 1 "user@test.com"   # 填写用户名
python3 scripts/cdp_ctl.py --tab-id {id} fill 2 "password123"     # 填写密码
python3 scripts/cdp_ctl.py --tab-id {id} click 3                   # 点击登录
python3 scripts/cdp_ctl.py --tab-id {id} wait text "欢迎"          # 等待登录成功
```

### 操作后查看触发的 API

```bash
python3 scripts/proxy_get_traffic.py --clear                      # 清空旧流量
python3 scripts/cdp_ctl.py --tab-id {id} click 5                   # 执行操作
python3 scripts/proxy_get_traffic.py --type api --summary          # 查看触发的 API
```

---

## 常见问题

| 问题 | 解决 |
|------|------|
| `CDP_ERROR: connect failed` | Chrome-SecTest 未运行，执行 `bash scripts/launch-chrome.sh` |
| 启动提示 `Port 9223 is occupied by a non-SecTest Chrome instance` | 9223 端口被你的个人 Chrome 占用。脚本会拒绝接管，避免误关你的页面。请改用其他 `CDP_PORT` 或释放 9223 |
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