# 漏洞 API 说明

## 数据结构

### Vulnerability

```json
{
  "id": "vuln_001",
  "title": "SQL 注入",
  "type": "SQL Injection",
  "severity": "critical",
  "description": "参数 id 存在时间盲注",
  "impact": "可获取数据库全部数据",
  "evidence": ["curl 输出原文", "截图路径"],
  "reproduction_steps": ["1. 访问 /api/search?id=1'", "2. 观察到响应延迟 5 秒"],
  "recommendation": "对参数 id 做参数化查询",
  "affected_scope": "/api/search 接口",
  "status": "confirmed",
  "source": "reason",
  "reported_by": "worker_1",
  "created_at": "2026-06-10T10:00:00Z"
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `title` | string | 漏洞标题 |
| `type` | string | 漏洞类型 |
| `severity` | enum | `info` / `low` / `medium` / `high` / `critical` |
| `description` | string | 详细描述 |
| `impact` | string | 实际影响 |
| `evidence` | string[] | 证据列表 |
| `reproduction_steps` | string[] | 复现步骤 |
| `recommendation` | string | 修复方案 |
| `affected_scope` | string | 影响范围 |
| `status` | enum | **审核状态** — `confirmed` / `pending` / `rejected` / `ignored` |
| `source` | string | 来源，固定 `"reason"` |
| `reported_by` | string | 上报的 worker 名称 |

## API 端点

### 1. 查询项目漏洞

```
GET /projects/{project_id}/vulns
```

返回 `Vulnerability[]`。

### 2. 新增漏洞

```
POST /projects/{project_id}/vulns
```

```json
{
  "title": "SQL 注入",
  "type": "SQL Injection",
  "severity": "high",
  "description": "...",
  "impact": "...",
  "evidence": ["..."],
  "reproduction_steps": ["..."],
  "recommendation": "...",
  "affected_scope": "...",
  "status": "confirmed",
  "source": "reason",
  "reported_by": "worker_1"
}
```

## 审核机制

**审核通过 `status` 字段实现，不需要单独的审核接口：**

```
reason 上报漏洞 → status = "confirmed" 或 "pending"
                    ↓
              下一轮 reason 读取图谱（含上一次的 vulns）
                    ↓
              vuln-review skill 要求模型审查已有 vuln
                    ↓
              证据不足 → 重新 POST 同 title，status = "rejected"
              需要确认 → POST，status = "pending"
              确认有效 → 保持 status = "confirmed"
```

status 取值：

| 值 | 含义 |
|----|------|
| `confirmed` | 证据直接证明漏洞存在 |
| `pending` | 有迹象但需额外确认 |
| `rejected` | 经审查为误报 |
| `ignored` | 已接受风险，无需处理 |

## 代码路径

| 文件 | 作用 |
|------|------|
| `server/models.py:154` | `Vulnerability` 模型定义 |
| `server/models.py:171` | `CreateVulnerabilityRequest` |
| `server/routers/projects.py:381` | `GET /projects/{id}/vulns` |
| `server/routers/projects.py:388` | `POST /projects/{id}/vulns` |
| `dispatcher/protocol/client.py:131` | `create_vulnerability()` 客户端调用 |
| `dispatcher/contracts.py:127` | `extract_reason_vulns()` 校验 LLM 输出的 vulns |
