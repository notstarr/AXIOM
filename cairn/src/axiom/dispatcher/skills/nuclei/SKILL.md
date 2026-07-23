---
name: nuclei
description: 当需要对目标进行自动化漏洞扫描时使用
---

## 工具

```bash
nuclei -u https://example.com
```

## 工具路径规范

- 始终使用规范命令名 `nuclei`，真实路径由 AXIOM Runner 统一注入 `PATH`。
- 只允许用 `command -v nuclei` 检查可用性；不可用时直接记录并跳过。
- 禁止猜测绝对路径，禁止使用 `find /` 搜索模板。
- 模板异常时只检查 Nuclei 自己报告的配置目录，不遍历整台机器。

## 常用参数

```bash
# 扫描单个目标
nuclei -u https://example.com

# 扫描多个目标（从文件）
nuclei -l urls.txt

# 从 stdin 读取
cat urls.txt | nuclei

# 指定输出文件
nuclei -u https://example.com -o results.txt

# JSON 格式输出（适合脚本处理）
nuclei -u https://example.com -json -o results.json

# 自动选择模板（根据目标技术栈）
nuclei -u https://example.com -as

# 指定严重程度
nuclei -u https://example.com -severity critical,high

# 指定模板类别
nuclei -u https://example.com -t cves/
nuclei -u https://example.com -t exposures/
nuclei -u https://example.com -t misconfiguration/
nuclei -u https://example.com -t vulnerabilities/

# 排除某些模板
nuclei -u https://example.com -etags brute-force,dos

# 并发控制
nuclei -u https://example.com -c 50 -rl 100  # 50 并发，100 请求/秒
```

## 输出示例

```
[cve-2021-44228] [http] [critical] https://example.com/?cmd=${jndi:ldap://test.com/a}
[exposed-panels] [http] [info] https://example.com/admin
[tls-misconfiguration] [http] [low] https://example.com
```

## 典型流程

```bash
# 1. subfinder + httpx 获取存活目标
subfinder -d example.com -silent | httpx -silent -o alive.txt

# 2. 全量扫描
nuclei -l alive.txt -o nuclei_results.txt

# 3. 只看高危
nuclei -l alive.txt -severity critical,high -o high_risk.txt

# 4. 针对特定 CVE
nuclei -l alive.txt -t cves/2024/ -o cve_2024.txt
```

## 注意事项

- `-as`（自动选择）会根据 httpx 的 tech-detect 结果选模板，减少噪音
- `--rate-limit` 控制请求速率，避免打挂目标
- 更新模板：`nuclei update-templates`
- 模板路径：`~/.local/nuclei-templates/`
- 大量目标时用 `-rl` 限速，避免触发 WAF
