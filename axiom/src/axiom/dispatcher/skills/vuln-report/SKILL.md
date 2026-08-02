---
name: vuln-report
description: 上报确认漏洞前阅读，包含去重、证据和通用字段规范
---

# Vulnerability reporting

本 Skill 只给 **Explore** 阶段使用。发现不等于漏洞：必须有明确的实际行为和影响，并按同一根因去重。每个漏洞都必须填写“利用前提”和“修复建议”；前者说明攻击者需要的身份、权限、环境或前置步骤，后者必须是可执行的服务端修复方向。应尽可能保存请求、响应和证明文件，但 Artifact Evidence 是可选审计材料，不得因为来不及补证据而阻塞 Explore 收口。Bootstrap 只记录线索；Reason 不得上报漏洞。

不要创建漏洞 JSON 文件，也不要使用旧的 `axiom-tool vulnerability report`。在当前 Explore lease 中，使用 Axiom CLI 逐条上报：

```bash
axiom explore vulnerability report \
  --title 'GET /api/users/{id} 未授权敏感信息泄露' \
  --type '未授权访问' \
  --severity high \
  --description '普通用户可读取其他用户的敏感资料' \
  --prerequisites '无需 Cookie 或 Authorization' \
  --impact '造成跨用户敏感数据泄露' \
  --remediation '在服务端实施认证和对象级授权' \
  --evidence-path 'evidence/request-01.txt' \
  --evidence-path 'evidence/response-01.txt' \
  --affected-target '127.0.0.1:18083'
```

长文本可用对应的 `--*-file` 参数；`--evidence-path` 可省略或重复。填写时必须指向当前 `{run_dir}/artifacts/evidence/` 下的安全相对路径；只有最终成功收集的文件才会建立证据关系。服务端会自动补齐 `intent_id`、`source=explore`、`reported_by` 和 `status=待验证`，并按当前 lease 校验、去重和持久化。

禁止上报纯推测、只看到配置、只有状态码或同根因重复项。漏洞叙述同步写入 `findings.md`，未完成方向写入 `leads.md`，已有原始请求、响应和证明可写入 `evidence/**`。不要为了补 Evidence 重新执行大型测试，也不要编辑 `explore_result.json`；阶段完成时由代码生成 Fact、正式 Artifact 和可用的证据关系。Task 开启外部上报时，Server 会经 Adapter 转换内部对象。
