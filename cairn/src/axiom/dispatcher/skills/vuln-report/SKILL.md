---
name: vuln-report
description: 上报确认漏洞前阅读，包含去重、证据和通用字段规范
---

# Vulnerability reporting

先运行 `axiom-tool vulnerability list`，按同一根因去重。发现不等于漏洞：必须有实际 payload、响应证据、影响说明和可复现步骤。

把通用漏洞对象写入当前私有运行目录的 JSON 文件，再运行：

```bash
axiom-tool vulnerability report --file "$AXIOM_RUN_DIR/vulnerability.json"
```

JSON 字段：

```json
{
  "intent_id": "i002",
  "title": "GET /api/users/{id} 越权访问",
  "type": "越权访问",
  "severity": "high",
  "description": "已验证的技术描述",
  "prerequisites": "利用前提",
  "impact": "已证明的实际影响",
  "reproduction_steps": ["步骤 1", "步骤 2"],
  "remediation": "修复建议",
  "curl_command": "完整可复现 curl",
  "evidence": ["Artifact ID 或证据索引"],
  "affected_target": "api.example.com",
  "status": "待验证",
  "source": "explore",
  "reported_by": "worker-name"
}
```

禁止上报推测、仅配置观察、缺少请求响应证据或同根因重复项。Task 开启外部上报时，Server 会复用同一内部对象经 Adapter 自动上报。
