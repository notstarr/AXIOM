---
name: vuln-report
description: 上报确认漏洞前阅读，包含去重、证据和通用字段规范
---

# Vulnerability reporting

本 Skill 只给 **Explore** 阶段使用。发现不等于漏洞：必须有实际请求/响应证据、明确影响、可复现步骤，并按同一根因去重。Bootstrap 只记录线索；Reason 不得上报漏洞。

不要创建漏洞 JSON 文件，也不要使用旧的 `axiom-tool vulnerability report`。在当前 Explore lease 中，使用 Axiom CLI 逐条上报：

```bash
axiom explore vulnerability report \
  --title 'GET /api/users/{id} 未授权敏感信息泄露' \
  --type '未授权访问' \
  --severity high \
  --description-file "$AXIOM_RUN_DIR/vuln-description.md" \
  --prerequisites '无需 Cookie 或 Authorization' \
  --impact-file "$AXIOM_RUN_DIR/vuln-impact.md" \
  --reproduction-step '发送请求：GET ...' \
  --reproduction-step '确认响应包含真实敏感数据' \
  --remediation '在服务端实施认证和对象级授权' \
  --curl-command 'curl -i ...' \
  --evidence '本轮证据文件名或摘要索引' \
  --affected-target '127.0.0.1:18083'
```

长文本可用对应的 `--*-file` 参数；列表参数可以重复。服务端会自动补齐 `intent_id`、`source=explore` 和 `reported_by`，并按当前 lease 校验、去重和持久化。`status=忽略` 必须同时提供原因。

禁止上报推测、只看到配置、只有状态码、缺少请求响应证据或同根因重复项。完成后把请求、响应和分析写入 `{run_dir}/artifacts/`，不要编辑 `explore_result.json`；阶段完成时由代码生成 Fact 和正式 Artifact。Task 开启外部上报时，Server 会复用同一内部对象经 Adapter 自动上报。
