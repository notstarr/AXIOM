---
name: httpx
description: 当需要探测 HTTP 服务存活、获取标题和状态码时使用
---

## 工具

```bash
httpx -l subdomains.txt
```

## 常用参数

```bash
# 从文件读取域名列表
httpx -l subdomains.txt

# 从 stdin 读取
cat subdomains.txt | httpx

# 静默模式（只输出存活 URL）
httpx -l subdomains.txt -silent

# 获取标题、状态码、技术栈
httpx -l subdomains.txt -title -status-code -tech-detect

//指定输出文件
httpx -l subdomains.txt -o alive.txt

//只看 200 状态码
httpx -l subdomains.txt -mc 200

//截图（需要 chromedp）
httpx -l subdomains.txt -screenshot

//探测特定端口
httpx -l subdomains.txt -p 80,443,8080,8443,3000
```

## 输出示例

```
http://www.example.com [200] [OK] [nginx]
https://api.example.com [200] [API] [Cloudflare]
http://dev.example.com [302] [Redirect] [Apache]
```

## 典型流程

```bash
# 1. subfinder 枚举子域名
subfinder -d example.com -all -silent -o subs.txt

# 2. httpx 探测存活
cat subs.txt | httpx -silent -title -status-code -tech-detect -o alive.txt

# 3. 将存活的 URL 传给 nuclei 扫描
cat alive.txt | nuclei -as
```

## 注意事项

- 默认探测 80 和 443 端口，用 `-p` 指定其他端口
- `-threads` 控制并发（默认 50），大量目标时调高
- `-follow-redirects` 跟随重定向
- `-no-color` 禁用颜色输出（适合脚本处理）