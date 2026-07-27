---
name: sqlmap
description: 当需要检测和利用 SQL 注入漏洞时使用
requirements:
  commands:
    - name: sqlmap
      args: [--version]
---

## 工具

```bash
sqlmap -u "https://example.com/api?id=1"
```

## 工具路径规范

- 始终使用规范命令名 `sqlmap`，真实路径由 AXIOM Runner 统一注入 `PATH`。
- 只允许用 `command -v sqlmap` 检查可用性；不可用时直接记录并跳过。
- 禁止猜测绝对路径，禁止通过 `find /` 搜索工具或 Python 依赖。

## 常用参数（高频）

```bash
# 基础检测
sqlmap -u "https://example.com/page?id=1"

# 指定参数检测（只测 id）
sqlmap -u "https://example.com/page?id=1" -p id

# POST 请求检测（表单）
sqlmap -u "https://example.com/login" --data="user=admin&pass=123"

# 从原始 HTTP 请求文件读取（-r）
sqlmap -r request.txt

# 带 Cookie
sqlmap -u "https://example.com/page?id=1" --cookie="session=abc123"

# 常用扫描控制（避免超时）
sqlmap -u "https://example.com/page?id=1" --level=1 --risk=1 --technique=BE --timeout=10 --retries=1 --batch
```

## JSON API 模板（推荐优先）

```bash
# 先用 curl 验证认证链路，再跑 sqlmap
curl -s -X POST "https://example.com/api/search" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -H "organization-code: <ORG_CODE>" \
  -d '{"keyword":"test","page":1}'

sqlmap -u "https://example.com/api/search" \
  --method POST \
  --data='{"keyword":"test","page":1}' \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -H "organization-code: <ORG_CODE>" \
  -p keyword \
  --level=1 --risk=1 --technique=BE \
  --timeout=10 --retries=1 --batch --flush-session
```

## -r 请求文件规范（关键）

`sqlmap -r` 读取的是“原始 HTTP 请求”，不是 curl 命令文本。`request.txt` 必须类似：

```http
POST /api/search HTTP/1.1
Host: example.com
Content-Type: application/json
Authorization: Bearer <TOKEN>
organization-code: <ORG_CODE>

{"keyword":"test","page":1}
```

要点：
- 第一行必须是 `METHOD PATH HTTP/1.1`
- 必须包含 `Host` 头
- 头和 body 之间必须有空行
- 不要写 shell 提示符、curl 命令、注释字符

## 推荐流程（避免“工具失败”误判）

- `--batch` 使用默认选项，不交互确认，AI 调用时必须加
- 先做认证验证：先 `curl` 跑通，再 `sqlmap`
- 先小后大：先 `--level=1 --risk=1 --technique=BE`，再逐步加深
- 参数收敛：优先用 `-p` 指定单参数，避免全参数扫描超时
- 控制时长：`--timeout=10 --retries=1`，长任务拆成多次短任务
- JSON API 优先 `-u + --method + --data`，`-r` 仅在你能提供标准 raw 请求时使用
- 若切换策略或参数，使用 `--flush-session` 清理历史缓存，避免旧结果干扰
- 仅在确认注入后再执行 `--dbs / --tables / --columns / --dump` 等深度动作
- `--tamper` 仅在怀疑 WAF 拦截时启用，如 `--tamper=space2comment,between`
- 结果保存在 `~/.local/share/sqlmap/output/` 下
