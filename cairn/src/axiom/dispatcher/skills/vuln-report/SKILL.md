---
name: vuln-report
description: 通过 API 上报漏洞前必须阅读，包含去重检查、证据规范和上报格式
disable-model-invocation: true
---

## 漏洞等级（severity）

上报时必须指定 severity，可选值：

critical, high, medium, low, info

具体评估标准见另一个技能：whatisvul

## 去重检查（上报前必做）

上报前 MUST 先查询已有漏洞列表，检查是否已有相同漏洞：

```bash
curl -s "${AXIOM_SERVER}/projects/${PROJECT_ID}/vulns" | python3 -c "
import sys, json
vulns = json.load(sys.stdin)
for v in vulns:
    print(v.get('id',''), v.get('title',''), v.get('status',''))
"
```

如果已有漏洞的标题与你要上报的漏洞描述**同一根因**，不要重复上报。不同表现但同一根因算一个漏洞。

## 漏洞 vs 发现

**发现不等于漏洞。** 使用以下判断树：

```
观察到异常
  ├── 是否成功构造并发送了攻击 payload？
  │     └── 否 → 这是发现，写入 fact，不要上报
  ├── 是否有响应证明危害真实发生？
  │     └── 否 → 这是发现，写入 fact，不要上报
  ├── 是否有完整证据链（payload + 响应 + 影响量化）？
  │     └── 否 → 标 severity: "info"，status: "pending"
  └── 全部满足 → 这是漏洞，按下方流程上报
```

## 用法

```bash
curl -X POST "${AXIOM_SERVER}/projects/${PROJECT_ID}/vulns" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "SQL 注入 - /api/search 可读取全量用户数据",
    "severity": "critical",
    "description": "参数 keyword 存在时间盲注。使用 payload admin'\'' OR 1=1-- 成功绕过登录，确认可读取 users 表全部 1.2 万行数据。",
    "prerequisites": "需要目标系统使用 MySQL 数据库且 keyword 参数未做参数化处理。",
    "impact": "攻击者可通过注入读取数据库中所有用户数据，包括密码哈希、手机号、身份证号。",
    "evidence": [
      "【正常请求】curl -X POST https://target/api/search -d \"keyword=test\" → HTTP 200, {\"results\": []}",
      "【攻击 payload】curl -X POST https://target/api/search -d \"keyword=admin' OR 1=1--\" → HTTP 200, {\"results\": [...全量数据...]}",
      "【影响验证】sqlmap 确认 users 表可读取，共计 12431 行"
    ],
    "reproduction_steps": [
      "1. 访问 /api/search?keyword=test 观察正常响应",
      "2. 注入 payload: admin'\'' OR 1=1--",
      "3. 响应返回所有用户数据，确认注入存在",
      "4. sqlmap 验证数据库级别影响"
    ],
    "status": "confirmed",
    "source": "explore",
    "reported_by": "worker-name"
  }'
```

## evidence 规范

每条 evidence 必须包含：

1. **攻击前状态** — 正常请求与响应
2. **攻击 payload** — 完整的 curl 命令或原始请求
3. **攻击后结果** — 响应原文片段，证明漏洞存在
4. **影响量化** — 读取了多少条数据 / 影响了哪个用户 / 获取了什么权限

## 禁止上报

- 仅有配置观察但未实际攻击验证
- "可能存在"、"或许有风险"等推测
- 同一个漏洞拆成多个重复上报

## 上报前自查

- [ ] 是否已查询已有漏洞列表，确认没有重复？
- [ ] 是否成功构造并发送了攻击 payload？
- [ ] 是否收到了证明危害的响应？
- [ ] 是否验证了影响是真实的（不是猜测）？
- [ ] evidence 是否包含完整的请求和响应？
- [ ] 有没有把同一个漏洞拆成多个？

## 其他
references/field-spec.md 获取漏洞规范