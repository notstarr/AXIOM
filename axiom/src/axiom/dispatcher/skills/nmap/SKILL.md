---
name: nmap
description: 当需要扫描端口、识别服务版本、检测操作系统时使用
requirements:
  commands:
    - name: nmap
      args: [--version]
      contains: Nmap version
---

## 工具

```bash
nmap example.com
```

## 工具路径与启动检查

- 始终使用规范命令名 `nmap`，真实路径由 AXIOM Runner 统一注入 `PATH`。
- 运行前只做轻量检查：

```bash
command -v nmap
nmap --version
```

- 如果输出包含 `Unable to find nmap-services`、`nmap-protocols` 或
  `nse_main.lua` 缺失，立即记录 `nmap_unavailable` 并停止 Nmap 方向。
- 禁止使用 `find /`、`find /Users` 或其他全盘搜索来寻找 Nmap 文件。
- 禁止猜测或硬编码 `.workbuddy`、`/usr/local/bin`、Homebrew 等机器路径。

## 常用参数

```bash
# 快速扫描 Top 1000 端口（默认）
nmap example.com

# 扫描指定端口
nmap -p 80,443,8080,8443,22,21,3306,6379 example.com

# 扫描全端口（1-65535，慢）
nmap -p- example.com

# 服务版本检测（关键——识别运行的具体服务）
nmap -sV example.com

# 操作系统检测
nmap -O example.com

# 综合扫描：服务版本 + 脚本 + OS
nmap -sV -sC -O example.com

# 快速扫描（跳过端口发现，直接扫常见端口）
nmap -F example.com

# Ping 扫描（只看主机是否存活）
nmap -sn 192.168.1.0/24

# UDP 扫描
nmap -sU example.com

# 指定速度（-T0 最慢 ~ -T5 最快，默认 -T3）
nmap -T4 -sV example.com

# 输出到文件
nmap -sV -oN scan_result.txt example.com
nmap -sV -oX scan_result.xml example.com  # XML 格式
```

## NSE 脚本引擎

nmap 内置 600+ 脚本，是渗透测试的核心能力：

```bash
# 默认脚本（等同 -sC）
nmap --script=default example.com

# 漏洞检测脚本
nmap --script=vuln example.com

# 特定服务枚举
nmap --script=smb-enum-shares,smb-enum-users example.com
nmap --script=mysql-info example.com -p 3306
nmap --script=http-enum,http-headers,http-methods example.com -p 80,443

# 暴力破解（需配合字典）
nmap --script=http-brute --script-args userdb=users.txt,passdb=passwords.txt example.com -p 80

# SSL/TLS 审计
nmap --script=ssl-cert,ssl-enum-ciphers example.com -p 443

# 列出所有可用脚本
nmap --script-help | grep http
```

## 输出示例

```
PORT     STATE SERVICE  VERSION
22/tcp   open  ssh      OpenSSH 8.9p1 Ubuntu
80/tcp   open  http     nginx 1.24.0
443/tcp  open  ssl/http nginx 1.24.0
3306/tcp open  mysql    MySQL 8.0.35
```

## 典型流程

```bash
# 1. 快速发现开放端口
nmap -F -T4 --host-timeout 60s example.com -oN ports.txt

# 2. 对开放端口做版本检测 + 默认脚本
nmap -sV -sC --host-timeout 60s -p 80,443,22 example.com -oN detailed.txt

# 3. 针对 Web 服务用 HTTP 脚本
nmap --script=http-enum,http-headers,http-methods,http-vuln* --host-timeout 60s -p 80,443 example.com
```

## 注意事项

- `-sV` 是最常用的参数——知道服务版本才能查已知漏洞
- `-T4` 是速度和准确性的平衡点，`-T5` 可能丢端口
- `-O` 需要 root/sudo 权限
- 不要把 `-F` 与 `-p` 同时使用；快速扫描使用 `-F`，指定端口时删除 `-F`
- NSE 脚本路径由 Nmap 自己管理，不要手工搜索或猜测路径
- 更新脚本库：`nmap --script-updatedb`
- 大范围扫描用 `--min-rate 1000` 提速
