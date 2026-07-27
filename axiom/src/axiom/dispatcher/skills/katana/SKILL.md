---
name: katana
description: 当需要爬取网站页面、发现隐藏路径和 API 端点时使用
requirements:
  commands:
    - name: katana
      args: [-version]
      contains: Current version
---

## 工具

```bash
katana -u https://example.com
```

## 工具路径规范

- 始终使用规范命令名 `katana`，真实路径由 AXIOM Runner 统一注入 `PATH`。
- 只允许用 `command -v katana` 检查可用性；不可用时直接记录并跳过。
- 禁止猜测绝对路径，禁止通过 `find /` 搜索工具或浏览器依赖。

## 常用参数

```bash
# 基础爬取
katana -u https://example.com

# 指定深度
katana -u https://example.com -d 3

# 无头浏览器模式（渲染 JS，发现动态内容）
katana -u https://example.com -headless

# 爬取并存储到文件
katana -u https://example.com -o crawled.txt

# JSON 输出（包含更多信息）
katana -u https://example.com -json -o crawled.json

# 只爬同域
katana -u https://example.com -domain-scope example.com

# 并发控制
katana -u https://example.com -c 20

# 限制爬取速率
katana -u https://example.com -rl 50

# 发现表单
katana -u https://example.com -form-extraction

# 从文件读取多个目标
katana -list urls.txt
```

## 输出示例

```
https://example.com/
https://example.com/login
https://example.com/api/v1/users
https://example.com/dashboard
https://example.com/assets/app.js
```

## 典型流程

```bash
# 1. 爬取目标站点
katana -u https://example.com -d 3 -headless -o endpoints.txt

# 2. 筛选出 API 端点
grep -E "/api/|/v[0-9]/" endpoints.txt > api_endpoints.txt

# 3. 用 httpx 探测 API 响应
cat api_endpoints.txt | httpx -status-code -title

# 4. 用 nuclei 扫描
cat endpoints.txt | nuclei -as
```

## 注意事项

- `-headless` 会启动 Chrome，速度慢但能发现 JS 渲染的内容
- 默认深度 3，深爬用 `-d 5` 或更高
- JS 文件中的 API 路径需要人工或脚本提取
- 与你的爬虫系统互补：katana 适合快速发现，爬虫系统适合深度业务建模
