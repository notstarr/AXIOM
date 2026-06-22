---
name: playwright-cli
description: 当需要截图留证或通过脚本控制浏览器时使用
---

## 多 Worker 隔离（必须遵守）

多个 worker 可能同时运行。Playwright 有两种模式，隔离策略不同：

### 模式 A：独立浏览器（推荐，无冲突）

```python
browser = p.chromium.launch(headless=True)  # 每次启动独立实例
page = browser.new_page()
# ... 操作 ...
browser.close()  # 用完必须关闭
```

多个 worker 各自 launch 互不干扰。适合截图、渲染验证、不需要登录态的场景。

### 模式 B：连接共享 Chrome（需要登录态时）

连接 Chrome-SecTest（port 9223，已登录目标系统）。**必须每次创建新 tab，用完关闭 tab，不要操作已有 tab**：

```python
browser = p.chromium.connect_over_cdp("http://localhost:9223")
context = browser.contexts[0]
page = context.new_page()           # ← 创建新 tab，不要用 contexts[0].pages[0]
page.goto("https://target.example.com")
page.screenshot(path="evidence.png")
page.close()                         # ← 只关 tab，不要 browser.close()
```

**禁止**：
- `browser.close()` — 会断开所有 worker 的 CDP 连接
- 操作 `context.pages[0]` — 这是其他 worker 可能正在使用的 tab

---

## 前置条件

```bash
source ~/browser-use-env/bin/activate
```

确保 Chrome-SecTest 已运行（模式 B 才需要）：
```bash
curl -s http://localhost:9223/json/list | python3 -c "import json,sys; tabs=json.load(sys.stdin); print(f'Chrome-SecTest: {len(tabs)} tab(s)')" 2>/dev/null || echo "Chrome-SecTest 未运行"
```

---

## 常见用法

### 截图留证（模式 A）

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://target.example.com")
    page.screenshot(path="evidence.png", full_page=True)
    browser.close()
```

### 截图并提取页面内容（模式 A）

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://target.example.com")
    title = page.title()
    text = page.inner_text("body")
    html = page.content()
    page.screenshot(path="screenshot.png")
    browser.close()
    print(f"Title: {title}")
    print(f"Text: {text[:500]}")
```

### 复用已登录 Chrome 截图（模式 B）

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9223")
    context = browser.contexts[0]
    page = context.new_page()            # 新 tab
    page.goto("https://target.example.com/dashboard")
    page.screenshot(path="dashboard.png", full_page=True)
    page.close()                          # 关 tab，不关 browser
```

### 执行 JavaScript（模式 B，复用登录态）

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9223")
    context = browser.contexts[0]
    page = context.new_page()
    page.goto("https://target.example.com")
    cookies = page.evaluate("document.cookie")
    localStorage = page.evaluate("JSON.stringify(localStorage)")
    page.close()
    print(f"Cookies: {cookies}")
    print(f"LocalStorage: {localStorage}")
```

### 运行脚本

```bash
source ~/browser-use-env/bin/activate
python3 my_script.py
```

---

## 证据截图规范

- 文件名格式：`{ai_dir}/evidence_{描述}.png`
- 截图前确保页面已完全加载：`page.wait_for_load_state("networkidle")`
- 全页截图：`full_page=True`
- 如果截图用于漏洞证据，确保 URL 栏或页面内容能看出目标地址

---

## 常见问题

| 问题 | 解决 |
|------|------|
| `connect_over_cdp` 失败 | Chrome-SecTest 未运行，用 xss-verify skill 的 `scripts/launch-chrome.sh` 启动 |
| 抢页面/tab 被其他人导航了 | 确认用了 `context.new_page()` 创建新 tab，不是复用已有 tab |
| `browser.close()` 后其他 worker CDP 断开 | 模式 B 下只 `page.close()`，永远不要 `browser.close()` |
| 截图空白 | 页面未加载完，加 `page.wait_for_load_state("networkidle")` |
