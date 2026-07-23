---
name: subfinder
description: 当需要枚举目标域名的子域名时使用
---

## 工具

```bash
subfinder -d example.com
```

## 工具路径规范

- 始终使用规范命令名 `subfinder`，真实路径由 AXIOM Runner 统一注入 `PATH`。
- 只允许用 `command -v subfinder` 检查可用性；不可用时直接记录并跳过。
- 禁止猜测绝对路径，禁止通过 `find /` 搜索工具。

## 常用参数

```bash
# 基础枚举
subfinder -d example.com

# 指定输出文件
subfinder -d example.com -o subdomains.txt

# 静默模式（只输出域名）
subfinder -d example.com -silent

# 使用所有被动源（默认已是被动模式）
subfinder -d example.com -all

# 指定并发
subfinder -d example.com -t 50

# 递归枚举（对发现的子域名继续枚举）
subfinder -d example.com -recursive
```

## 输出示例

```
www.example.com
api.example.com
dev.example.com
mail.example.com
```

## 典型流程

```bash
# 1. 枚举子域名
subfinder -d example.com -all -o subdomains.txt

# 2. 用 httpx 探测存活的 HTTP 服务
cat subdomains.txt | httpx -silent -o alive.txt
```

## 注意事项

- 被动枚举，不向目标发请求，不会触发 WAF/IDS
- 结果取决于被动数据源，不一定全
- 如需更全面的枚举，配合 amass 使用
