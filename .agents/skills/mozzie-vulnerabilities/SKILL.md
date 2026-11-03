---
name: mozzie-vulnerabilities
description: Use this skill to operate Mozzie's vulnerability management API: authenticate with an API key, list and inspect vulnerability findings, update editable finding content and status under the same project and role rules as the Mozzie web API, and create findings when authorized. This is an operations skill for the Mozzie security platform, not a code-fixing or remediation skill.
---

# Mozzie Vulnerability Operations

This skill lets an AI agent interact with the Mozzie security platform's vulnerability findings and administrator-only Ingress domain inventory. It is intended for vulnerability intake, triage context retrieval, content and status updates, reporting newly discovered findings, and looking up deployed domains and their backing services.

**This is not a vulnerability remediation skill.** Do not claim to fix code, edit repositories, or perform code changes through this skill.

## Security and authentication

Use the API key only through environment variables or another secret store. Never print it, put it in source code, commit it, or include it in a final response.

```bash
export MOZZIE_BASE_URL='https://mozzie.example.com'
export MOZZIE_API_KEY='mozzie_live_...'
```

Send the key with every request:

```http
Authorization: Bearer ${MOZZIE_API_KEY}
```

Each API key inherits the Mozzie project permissions of its owner. Administrators can read and update all non-deleted findings; other users can read and update only findings in projects they can access.

## Capabilities and boundaries

### Supported

- List vulnerability findings with pagination and filters.
- Retrieve a single vulnerability finding by ID.
- Update an existing finding's editable content and status, subject to the same project and role rules as the Mozzie web API.
- Record that development remediation is complete by changing a finding to `fixed` with a remediation reason.
- When the same Agent has completed the requested code remediation and validation, automatically record the finding as `fixed` with the validation evidence.
- Create a new vulnerability finding when the API key belongs to an administrator.
- Query Ingress domain records when the API key belongs to an administrator.

### Not supported

- Updating an existing finding's SCM provider, source, creator, or route.
- Claiming, fixing code, verifying by code, or deleting findings through this skill.
- Managing projects, users, API keys, or repository permissions.

If a requested action is not supported, explain the limitation and provide the finding ID and the required information for a human operator.

## API operations

### Verify access and list findings

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer ${MOZZIE_API_KEY}" \
  "${MOZZIE_BASE_URL}/api/agent/v1/vulnerabilities?limit=20&status=pending,fixing"
```

Supported query parameters:

- `page`: 1-based page number; default `1`.
- `limit`: number of results from `1` to `100`; default `20`.
- `ids`: comma-separated vulnerability IDs, up to 500 IDs, for example `ids=101,102,103`.
- `search`: searches the vulnerability name, project, and description.
- `project`: exact project path.
- `status`: comma-separated status values.
- `severity`: comma-separated values: `低危`, `中危`, `高危`, `严重`.
- `updatedSince`: ISO-8601 timestamp for incremental retrieval.

Response shape:

```json
{
  "success": true,
  "data": {
    "vulnerabilities": [],
    "pagination": { "page": 1, "limit": 20, "total": 0, "pages": 1 }
  }
}
```

### Retrieve a finding

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer ${MOZZIE_API_KEY}" \
  "${MOZZIE_BASE_URL}/api/agent/v1/vulnerabilities/<id>"
```

A finding may include its project, SCM provider, vulnerability name and type, severity, status, source, description, reproduction curl command, route context, creator, fixer, and timestamps. Access is filtered by the API key owner's project permissions.

### Update a finding

Use the same authorization model as the original Mozzie vulnerability update API:

- Administrators may update findings in any project.
- Non-administrators may update findings only when they have access to the finding's current project.
- When a non-administrator changes `project`, they must also have access to the target project.
- Only administrators may set `verified` or `duplicate`.
- Setting `false_positive`, `no_risk`, or `deferred` requires a non-empty `reason`, except when transitioning from an AI status to a closure status as allowed by the original API.
- Deleted findings cannot be updated.

```bash
curl --fail-with-body -sS -X PUT \
  -H "Authorization: Bearer ${MOZZIE_API_KEY}" \
  -H "Content-Type: application/json" \
  "${MOZZIE_BASE_URL}/api/agent/v1/vulnerabilities/<id>" \
  -d '{
    "vulnName": "Broken object level authorization in order detail API",
    "severity": "高危",
    "description": "Updated impact, affected endpoint, and reproducible evidence.",
    "curlCommand": "curl ...",
    "status": "fixed",
    "fixer_id": "42",
    "reason": "The finding details, fixer assignment, and status were updated after remediation verification."
  }'
```

The request body accepts one or more editable fields:

- `vulnName`
- `vulnType`
- `project`
- `severity`
- `description` (`null` clears the field)
- `curlCommand` (`null` clears the field)
- `status`
- `fixer_id` or `fixerId` (set a fixer by user ID; `null` clears the assignment)
- `reason` for the audit record or a status transition that requires justification

The `fixer_id` and `fixerId` aliases must not conflict when both are provided. The API validates that the target user exists. Administrators may assign any existing user; non-administrators follow the same project authorization rules as the other finding updates.

At least one editable field other than `reason` is required. Supported statuses are `pending`, `fixing`, `fixed`, `verified`, `false_positive`, `no_risk`, `duplicate`, `deferred`, `ai_confirmed`, `ai_false_positive`, `ai_uncertain`, `ai_fixed`, `ai_fixing`, and `deleted`.

All content, fixer assignment, and status changes are written to the Mozzie audit log. The response includes the updated finding summary. This operation updates the Mozzie record only; it does not modify repository code.

### Record a completed remediation

After developers have completed the requested code remediation, update the Mozzie finding to `fixed`. Testing is a security validation activity and is recommended, but it is not a prerequisite for this status update. Before changing it, retrieve the latest finding so the update uses the current ID and status. Include a concise `reason` describing the remediation and include test or review evidence when available.

```bash
curl --fail-with-body -sS -X PUT \
  -H "Authorization: Bearer ${MOZZIE_API_KEY}" \
  -H "Content-Type: application/json" \
  "${MOZZIE_BASE_URL}/api/agent/v1/vulnerabilities/<id>" \
  -d '{
    "status": "fixed",
    "reason": "修复了订单详情接口的对象级授权校验，并通过回归测试验证未授权用户无法访问其他订单。"
  }'
```

Use `fixing` when remediation has started and `fixed` immediately after the requested code change is completed. Use `verified` only when an administrator has independently confirmed the fix; changing code or running a developer's tests alone does not grant permission to set `verified`. If the code remediation itself is incomplete, keep the current status and report the finding ID and remaining work instead of closing it.

### Create a finding

Only an administrator API key may create findings.

```bash
curl --fail-with-body -sS -X POST \
  -H "Authorization: Bearer ${MOZZIE_API_KEY}" \
  -H "Content-Type: application/json" \
  "${MOZZIE_BASE_URL}/api/agent/v1/vulnerabilities" \
  -d '{
    "vulnName": "Broken object level authorization",
    "vulnType": "越权访问",
    "project": "group/project",
    "severity": "高危",
    "description": "Describe the impact, affected endpoint, preconditions, and reproducible evidence.",
    "curlCommand": "curl ...",
    "scmProvider": "gitlab",
    "source": "codex",
    "fixer_id": "42"
  }'
```

Required fields: `vulnName`, `vulnType`, `project`, and `severity`.

Optional fields: `description`, `curlCommand`, `scmProvider`, `source`, and `fixer_id` (the API also accepts the camelCase alias `fixerId`).

`fixer_id` must reference an existing Mozzie user. The endpoint validates the user before creating the finding.

When a finding is created:

- `status` is automatically set to `pending`.
- `source` defaults to `codex` when omitted and can be explicitly provided from the supported source values.
- `creator_id` is automatically set to the authenticated administrator's user ID.
- `fixer_id` is set when provided.
- The existing finding is never modified.

Only create a finding when the user explicitly asks you to report one or when you have concrete, reproducible evidence of a new issue. Avoid duplicate reports by checking existing findings first when practical.

### Query domains

Only an administrator API key may query the Ingress domain inventory. Use the domain endpoint to find a host and inspect its environment, Ingress, namespace, service, deployment, project, and branch mapping:

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer ${MOZZIE_API_KEY}" \
  "${MOZZIE_BASE_URL}/api/agent/v1/ingress-info?host=api.example.com"
```

Supported query parameters:

- `page`: 1-based page number; default `1`.
- `limit`: number of records from `1` to `100`; default `20`.
- `host`: partial host match.
- `search`: searches host, service name, Ingress name, and project.
- `env`: exact environment value.
- `project`: exact project or comma-separated projects.
- `branch`: exact branch.

The response shape is:

```json
{
  "success": true,
  "data": {
    "ingressInfos": [],
    "pagination": { "page": 1, "limit": 20, "total": 0, "pages": 1 }
  }
}
```

Domain queries are read-only and do not create or modify vulnerability findings.

## Operating procedure

1. Load `MOZZIE_BASE_URL` and `MOZZIE_API_KEY` from the runtime environment without exposing the secret.
2. Verify access by listing findings with a narrow filter when possible.
3. Use the list endpoint for discovery and the detail endpoint for the complete record.
4. Before updating a finding, retrieve its latest record, change only the fields requested by the user, confirm any status transition is allowed, and include a reason when required.
5. When a connected development workflow finishes the requested code fix, automatically call the update endpoint immediately to set `status` to `fixed`. Include a concise reason; add test or validation evidence when it is available, but do not wait for tests before making this status update. This automatic post-fix update applies only when the Agent performed or directly observed the remediation in the same task.
6. For a reporting request, check for likely duplicates before creating a finding.
7. Create a finding only with complete, evidence-based information and only if the key is an administrator key.
8. Report the operation result, including the finding ID and status, but never report the API key.

Do not turn this workflow into a code remediation workflow. If the user asks to fix a vulnerability, this skill may provide the finding context, but another tool or workflow must handle code changes and tests.

## Error handling

- `401 Invalid API Key`: the key is missing, malformed, revoked, or expired. Do not print the key; ask the user to configure a valid secret.
- `403`: the API key lacks project access, or a non-administrator attempted to set `verified` or `duplicate`.
- `404 漏洞不存在或无权访问`: the finding does not exist, is deleted, or is outside the key owner's project scope.
- `429`: the rate limit was reached. Honor `Retry-After` and retry later.
