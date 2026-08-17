---
name: browser-control
description: 使用 AXIOM 原生浏览器工具读取和操作已登录的 Chrome-SecTest 页面
requirements:
  python_modules: [playwright]
---

# Browser Control

浏览器控制只是测试的一种手段，主要用于触发真实业务流程并取得真实参数和值；对于已经明确的接口，优先使用 `curl` 等直接、可复现的方式验证。

Explore 启用本 Skill 后，直接使用 AXIOM 内置 Extension 注册的 `browser_*` 工具。本 Skill 只是使用指南，不是工具实现；Chrome 生命周期、CDP 连接、页面隔离、网络记录和 Lease 清理都由 Browser Broker 管理。Chrome 未运行时，第一次调用浏览器工具会由 Broker 自动启动受控的 Chrome-SecTest。

浏览器工具是 Explore 阶段固定的内置 Tool，不进入阶段能力绑定表，也不需要在页面单独启停。**不要**通过 Bash 调用任何 `browser-control/scripts/`、`cdp_ctl.py`、启动 Chrome、拼接 CDP 命令或读取全局 mitmproxy/Reqable 流量。原生工具不可用时应报告基础设施错误，不得回退到可能操作其他 Worker 页面的旧路径。小程序和 App 的原生流量不属于本 Skill 范围。

## 默认流程

```text
browser_open_page
→ browser_read_page 或 browser_snapshot
→ 使用 Snapshot 返回的准确 Ref 操作
→ browser_wait 或 browser_snapshot(scope="changes") 验证结果
```

1. 首次访问 URL 使用 `browser_open_page`，保存返回的 `page_id`。
2. 只读取文本和链接时使用 `browser_read_page`。
3. 需要点击、填表或检查控件时，先执行一次 `browser_snapshot`。
4. 从完整快照中选择准确 `ref`，并确认目标动作存在于 `allowed_actions`。
5. 操作完成仅代表事件已派发。关键结果必须按文本、URL、Ref、加载状态或下载验证。

不要重复打开已经存在的 URL。需要恢复页面时使用 `browser_list_pages`，它只会返回当前 Lease 的页面。

## Ref 与 Snapshot

Snapshot Ref 是可访问性记录，不是 CSS/XPath，也不是长期稳定的数据库 ID。记录只包含执行需要的字段：

- `ref`
- `role`
- `accessible_name`
- `text`
- `attributes`
- `allowed_actions`

支持以下 Scope：

- `interactive`：默认，交互节点和必要上下文；
- `viewport`：当前视口文本和交互结构；
- `subtree`：指定 Ref 的子树；
- `full`：完整页面骨架；
- `changes`：相对上一次兼容快照的变化。

导航会使旧 Ref 全部失效。SPA 重绘、人工操作或出现 `stale_ref/ref_not_found` 时，重新执行一次 `browser_snapshot`，不要猜测新 Ref，也不要改用文本模糊点击。

页面过大且返回 `truncated=true` 时，通过滚动、`viewport` 或 `subtree` 缩小观察范围。

## 常用操作

- 点击：`browser_click`
- 填写：`browser_fill`
- 指定元素按键：`browser_press`（必须使用 Snapshot Ref）
- 悬停菜单：`browser_hover`
- 页面滚动或将 Ref 滚动到视口：`browser_scroll`
- 下拉选择：`browser_select`
- 勾选或取消：`browser_check`
- 上传当前运行目录内文件：`browser_upload_file`

`browser_evaluate` 仅用于聚焦的应用内检查，不得用 JavaScript 替代正常读取、Snapshot 和 Ref 交互。

## 等待与验证

`browser_wait` 支持：

- `text`
- `url`
- `load_state`
- `ref`
- `download`
- `time`

优先等待最窄、可观察的成功条件。打开普通页面后不要机械等待；提交、登录、搜索和保存后先读取当前页面，目标尚未出现时再等待具体条件。

## 网络、Console 与截图

页面网络记录由 Browser Broker 在当前 Lease 内监听 Playwright 页面事件。需要观察某个动作产生的请求时，先读取当前游标，再执行动作，最后用新游标读取增量：

```text
browser_read_network(page_id, after_cursor=当前 cursor)
→ browser_click / browser_fill / browser_press
→ browser_wait（必要时等待 text、url 或 load_state）
→ browser_read_network(page_id, after_cursor=上一步返回的 cursor)
```

- `browser_read_network` 按游标增量读取当前页面的 HTTP 和 WebSocket 活动，不会清空其他 Worker 数据。
- `browser_read_console` 按游标读取 Console、Page Error 和 Dialog。
- `browser_get_credentials` 从受控浏览器提取指定主域（如 `qq.com` 或 `*.qq.com`）或子域的 Cookie（含 HttpOnly）与 Storage，用于 API 探测或会话续期。
- 敏感 Header 默认脱敏；只有验证确实需要时才设置 `include_sensitive=true`。
- `browser_screenshot` 默认保存到本轮 `process/browser/screenshots/`；需要正式证据时显式提供当前运行目录内的路径。
- `labels=true` 会在截图中标注当前 Snapshot 的 Ref；标记只对当时页面状态有效。

完整浏览器日志保存在当前 Lease 的 `process/browser/`，不要读取或清理 `/tmp/proxy_flows.jsonl`。

## 人工验证与安全边界

出现验证码、异常流量警告或人工验证时：

Chrome-SecTest 窗口默认最小化运行，AI 操作不抢占前台焦点；需要人工介入时，从 Dock 恢复窗口查看和操作即可，恢复后 AI 不会再次强制最小化。

1. 立即停止自动化交互，不绕过或脚本化验证；
2. 保持页面打开，通过 Explore 对话说明需要的人工操作；
3. 人工处理完成后重新读取页面并获取新 Snapshot；
4. 不复用人工操作前的 Ref。

浏览器共享登录 Profile，因此 Cookie 状态跨页面共享；页面控制权按 Lease 隔离。只能操作当前 Task Scope 和当前 Browser Lease 返回的 `page_id`。
