# browser-use CLI 命令速查

## 会话管理

```bash
browser-use --session <name> open <url>        # 打开/跳转 URL
browser-use --session <name> state             # 获取页面状态（URL、标题、可交互元素）
browser-use --session <name> screenshot <path> # 全页截图
browser-use --session <name> close             # 关闭会话
browser-use --session <name> list              # 列出所有会话
```

## 元素操作

```bash
browser-use --session <name> click "<selector>"       # 点击元素
browser-use --session <name> type "<selector>" "text" # 输入文本
browser-use --session <name> scroll down              # 向下滚动
browser-use --session <name> scroll up                # 向上滚动
browser-use --session <name> wait url "<pattern>"     # 等待 URL 匹配
```

## 数据提取

```bash
browser-use --session <name> extract "提取页面主要内容"  # LLM 辅助提取
browser-use --session <name> eval "document.title"      # 执行 JS
```

## CDP 模式（复用已登录浏览器）

```bash
browser-use --cdp-url http://localhost:9223 open <url>
browser-use --cdp-url http://localhost:9223 state
browser-use --cdp-url http://localhost:9223 extract "页面主要内容"
```

## 选择器语法

| 语法 | 匹配 | 示例 |
|------|------|------|
| `text:可见文字` | 按文字匹配 | `text:登录` |
| `xpath://...` | XPath | `xpath://button[@id="submit"]` |
| `css:selector` | CSS 选择器 | `css:.btn-primary` |
| `input:placeholder` | 按 placeholder | `input:用户名` |

## 常见问题

- 会话在命令间**持续保持**，不需要每次重新 open
- `extract` 使用 LLM 解析，适合提取复杂结构化数据
- `eval` 直接执行 JS，适合精确操作
- 截图保存在**当前工作目录**，建议指定绝对路径
