---
name: vuln-review
description: 审查已有漏洞声明——拉取漏洞列表，逐条核查证据，修正误报
disable-model-invocation: true
---

## 用法

先用 `curl` GET 拉取已有漏洞列表，逐条审查，发现误报或需要修正的再 POST 更新：

### 1. 拉取漏洞列表

```bash
curl -s "${AXIOM_SERVER}/projects/${PROJECT_ID}/vulns"
```

返回 JSON 数组，每条格式同 vuln-report skill 中的定义。

### 2. 逐条审查

对每条漏洞检查：
- `evidence` 是否直接支撑结论？
- `severity` 是否匹配实际影响？
- `reproduction_steps` 是否可复现？
- `recommendation` 是否具体可操作？

### 3. 提交修正

证据不足 → POST 同 title，`status: "pending"`  
确认为误报 → POST 同 title，`status: "rejected"`，description 说明原因  
严重程度不匹配 → POST 同 title，修正 `severity`  
证据充分且无问题 → 无需操作，保持 `status: "confirmed"`

```bash
curl -X POST "${AXIOM_SERVER}/projects/${PROJECT_ID}/vulns" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "SQL 注入 - /api/search",
    "type": "SQL Injection",
    "severity": "medium",
    "description": "原报 high，经核查仅返回非敏感数据，降为 medium",
    "impact": "仅泄露查询计数，不包含用户数据",
    "evidence": ["curl 输出为 count=42，无用户字段"],
    "reproduction_steps": ["..."],
    "recommendation": "...",
    "affected_scope": "/api/search",
    "status": "rejected",
    "source": "reason",
    "reported_by": "worker-name"
  }'
```

## 规则

- 必须先 GET 再审查，不要凭记忆判断
- 每轮 reason 都应该审查上一轮上报的漏洞
- 误报必须 POST `status: "rejected"`，不要置之不理
- 同标题多次 POST 会新增多条记录，前端以最新一条的 status 为准