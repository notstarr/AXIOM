---
name: browser-use-cli
description: 当需要操作浏览器（访问网页、点击元素、填写表单、截图）时使用
---

## ⚠️ 多 Worker 隔离（必须遵守）

多个 worker 可能同时运行，默认 session 是共享的会互相干扰。**每次调用 browser-use 命令时必须加 `--session` 参数**，使用当前 intent ID 作为 session 名：

```bash
# 正确：每个 worker 用独立 session
browser-use --session i042 open https://example.com
browser-use --session i042 state
browser-use --session i042 click 5

# 错误：不加 --session，多 worker 会抢同一个浏览器
browser-use open https://example.com
```

Session 名建议格式：`{intent_id}`，例如 `i042`、`i105`。

**任务完成后必须关闭，释放浏览器进程：**
```bash
browser-use --session i042 close
```

---

## 前置条件

每次使用前确保环境已激活：

```bash
source ~/browser-use-env/bin/activate
```

---

## 核心命令速查

### 导航

```bash
browser-use open https://example.com       # 打开/跳转 URL
browser-use back                           # 返回上一页
```

### 查看页面状态（必须先执行，再决定点哪里）

```bash
browser-use state                          # 查看当前 URL、标题、所有可交互元素（带编号）
browser-use screenshot page.png            # 截图，保存到当前目录
```

### 交互

```bash
browser-use click 5                        # 点击编号为 5 的元素
browser-use click 120 450                  # 点击坐标 (x=120, y=450)
browser-use dblclick 3                     # 双击
browser-use hover 7                        # 悬停
browser-use type "Hello World"             # 在当前焦点输入文字
browser-use input 2 "search term"          # 清空后向第 2 号输入框输入
browser-use select 4 "option text"         # 选择下拉框选项
browser-use keys "Enter"                   # 发送键盘事件（Tab, Enter, Escape 等）
browser-use scroll down 3                  # 向下滚动 3 次
browser-use scroll up 2                    # 向上滚动
```

### 提取数据

```bash
browser-use extract "所有商品名称和价格"       # 用自然语言描述要提取什么，LLM 负责解析
browser-use eval "document.title"            # 执行 JS 并返回结果
browser-use eval "document.cookie"           # 获取 cookies
browser-use get url                          # 获取当前 URL
browser-use get title                        # 获取页面标题
browser-use get html                         # 获取完整 HTML 源码
browser-use get text                         # 获取页面可见文字
```

### 标签页管理

```bash
browser-use tab list                        # 列出所有标签页
browser-use tab switch 1                    # 切换到第 1 个标签页
browser-use tab close                       # 关闭当前标签页
```

### 其他

```bash
browser-use wait url "https://target.com"  # 等待跳转到目标 URL
browser-use wait text "加载完成"             # 等待页面出现特定文字
browser-use cookies get                     # 获取当前页面所有 cookie
browser-use upload 3 /path/to/file.pdf      # 上传文件到第 3 号文件输入框
browser-use close                           # 关闭浏览器
```

---

## 标准操作流程

### 打开页面并获取元素

```bash
browser-use open https://target.com        # 1. 打开页面
browser-use state                          # 2. 查看元素编号
browser-use screenshot before.png         # 3. 可选：截图留证
browser-use click 5                        # 4. 点击目标元素
browser-use state                          # 5. 确认操作结果
```

### 填写表单并提交

```bash
browser-use open https://target.com/login
browser-use state                          # 查看输入框编号
browser-use input 1 "username@email.com"   # 填写用户名
browser-use input 2 "password"             # 填写密码
browser-use click 3                        # 点击登录按钮
browser-use wait text "欢迎"               # 等待登录成功
```

### 使用已登录的真实 Chrome（复用登录态）

```bash
# 连接到已开启调试端口的 Chrome（port 9223 = Chrome-SecTest）
browser-use --cdp-url http://localhost:9223 open https://target.com
browser-use --cdp-url http://localhost:9223 state
browser-use --cdp-url http://localhost:9223 extract "页面主要内容"
```

---

## 重要提示

- **每次操作前先运行 `browser-use state`**，获取元素编号，不要凭猜测点击
- 浏览器会话在命令间**持续保持**，不需要每次重新 `open`
- `extract` 使用 LLM 解析，适合提取复杂结构化数据；`eval` 直接执行 JS，适合精确操作
- 如需使用已登录的目标系统账号，加 `--cdp-url http://localhost:9223` 连接 Chrome-SecTest
- 截图会保存在**当前工作目录**，建议指定绝对路径
