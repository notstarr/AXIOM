---
name: playwright-cli
description: 当需要对网页截图或通过 Python 脚本控制浏览器时使用。适合截图留证、渲染验证。持续交互操作（点击/填表）请用 browser-use-cli。
---

## 前置条件

```bash
source ~/browser-use-env/bin/activate
```

## CLI 命令

```bash
# 截图（最常用）
playwright screenshot https://example.com output.png
playwright screenshot --full-page https://example.com full.png   # 完整页面

# 录制操作并生成 Python 代码
playwright codegen -o my_script.py https://example.com
```

---

## Python 脚本（需要复杂控制时）

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://example.com")
    page.screenshot(path="screenshot.png", full_page=True)
    content = page.inner_text("body")
    title = page.evaluate("document.title")
    browser.close()
```

### 复用已登录 Chrome（已认证场景）

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    # 连接 Chrome-SecTest（port 9223，已登录目标系统）
    browser = p.chromium.connect_over_cdp("http://localhost:9223")
    page = browser.contexts[0].new_page()
    page.goto("https://target.example.com")
    page.screenshot(path="evidence.png", full_page=True)
    # 不要 browser.close()
```

```bash
source ~/browser-use-env/bin/activate
python3 my_script.py
```

