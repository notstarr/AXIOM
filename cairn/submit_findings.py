import json
import os
import urllib.request
import urllib.error

BASE = "http://localhost:8888"
MOZZIE = "https://mozzie.t.cn-shenzhen.aliyun.kkgroup.work"
MOZZIE_TOKEN = os.getenv("MOZZIE_TOKEN", "")
PROJECT_ID = "proj_065"
GROUP_ID = "pg_009"
SOURCE_FILE = "/tmp/axiom-files/pg_009/proj_065/ai/i003_findings.md"
DOMAIN = "authority-manage-service.saas-test.cn-beijing.volce.teamshare.work"

if not MOZZIE_TOKEN:
    raise SystemExit("MOZZIE_TOKEN is required (export MOZZIE_TOKEN before running submit_findings.py)")

def post(url, data, headers=None):
    headers = headers or {}
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")

assets = [
    {
        "project_id": PROJECT_ID,
        "group_id": GROUP_ID,
        "api_path": "/permissions/tenants",
        "api_method": "GET",
        "backend_domain": DOMAIN,
        "api_purpose": "查询全部租户权限配置（resource/operator/type），可返回264个租户/12,848条权限",
        "auth_required": "yes",
        "auth_type": "JWT",
        "test_status": "vulnerable",
        "source_file": SOURCE_FILE,
        "sample_body": None,
        "content_type": None
    },
    {
        "project_id": PROJECT_ID,
        "group_id": GROUP_ID,
        "api_path": "/token/tenants",
        "api_method": "GET",
        "backend_domain": DOMAIN,
        "api_purpose": "查询DT001组织39个租户列表",
        "auth_required": "yes",
        "auth_type": "JWT",
        "test_status": "vulnerable",
        "source_file": SOURCE_FILE,
        "sample_body": None,
        "content_type": None
    },
    {
        "project_id": PROJECT_ID,
        "group_id": GROUP_ID,
        "api_path": "/organization/tenants/queries",
        "api_method": "POST",
        "backend_domain": DOMAIN,
        "api_purpose": "查询DT001组织租户信息，可返回4条租户信息",
        "auth_required": "yes",
        "auth_type": "JWT",
        "test_status": "vulnerable",
        "source_file": SOURCE_FILE,
        "sample_body": None,
        "content_type": "application/json"
    },
    {
        "project_id": PROJECT_ID,
        "group_id": GROUP_ID,
        "api_path": "/v3/organization/staffs/queries",
        "api_method": "POST",
        "backend_domain": DOMAIN,
        "api_purpose": "查询DT001组织员工姓名+部门+头像，可返回4,696条员工PII",
        "auth_required": "yes",
        "auth_type": "JWT",
        "test_status": "vulnerable",
        "source_file": SOURCE_FILE,
        "sample_body": None,
        "content_type": "application/json"
    },
    {
        "project_id": PROJECT_ID,
        "group_id": GROUP_ID,
        "api_path": "/v3/organization/chart",
        "api_method": "GET",
        "backend_domain": DOMAIN,
        "api_purpose": "查询DT001完整组织架构树，可返回33个一级部门",
        "auth_required": "yes",
        "auth_type": "JWT",
        "test_status": "vulnerable",
        "source_file": SOURCE_FILE,
        "sample_body": None,
        "content_type": None
    },
    {
        "project_id": PROJECT_ID,
        "group_id": GROUP_ID,
        "api_path": "/departments/queries",
        "api_method": "POST",
        "backend_domain": DOMAIN,
        "api_purpose": "查询DT001组织部门信息，可返回165条部门信息",
        "auth_required": "yes",
        "auth_type": "JWT",
        "test_status": "vulnerable",
        "source_file": SOURCE_FILE,
        "sample_body": None,
        "content_type": "application/json"
    },
    {
        "project_id": PROJECT_ID,
        "group_id": GROUP_ID,
        "api_path": "/v3/organization/partners/queries",
        "api_method": "POST",
        "backend_domain": DOMAIN,
        "api_purpose": "查询DT001组织合作伙伴信息，可返回14条含手机号的敏感数据",
        "auth_required": "yes",
        "auth_type": "JWT",
        "test_status": "vulnerable",
        "source_file": SOURCE_FILE,
        "sample_body": None,
        "content_type": "application/json"
    },
    {
        "project_id": PROJECT_ID,
        "group_id": GROUP_ID,
        "api_path": "/user/{id}/power",
        "api_method": "GET",
        "backend_domain": DOMAIN,
        "api_purpose": "查询用户在多个租户的角色分配，可返回高权限用户在13个租户的角色信息（含super_admin）",
        "auth_required": "yes",
        "auth_type": "JWT",
        "test_status": "vulnerable",
        "source_file": SOURCE_FILE,
        "sample_body": None,
        "content_type": None
    },
    {
        "project_id": PROJECT_ID,
        "group_id": GROUP_ID,
        "api_path": "/business-user/roles/{roleId}/users-groups",
        "api_method": "PUT",
        "backend_domain": DOMAIN,
        "api_purpose": "修改角色用户组关联",
        "auth_required": "yes",
        "auth_type": "JWT",
        "test_status": "safe",
        "source_file": SOURCE_FILE,
        "sample_body": None,
        "content_type": "application/json"
    },
    {
        "project_id": PROJECT_ID,
        "group_id": GROUP_ID,
        "api_path": "/user/{userId}/power",
        "api_method": "POST",
        "backend_domain": DOMAIN,
        "api_purpose": "修改用户角色权限",
        "auth_required": "yes",
        "auth_type": "JWT",
        "test_status": "safe",
        "source_file": SOURCE_FILE,
        "sample_body": None,
        "content_type": "application/json"
    },
    {
        "project_id": PROJECT_ID,
        "group_id": GROUP_ID,
        "api_path": "/business-user/groups/{groupId}/roles",
        "api_method": "PUT",
        "backend_domain": DOMAIN,
        "api_purpose": "修改用户组角色关联",
        "auth_required": "yes",
        "auth_type": "JWT",
        "test_status": "safe",
        "source_file": SOURCE_FILE,
        "sample_body": None,
        "content_type": "application/json"
    },
    {
        "project_id": PROJECT_ID,
        "group_id": GROUP_ID,
        "api_path": "/role/{roleId}/power/{tenantId}",
        "api_method": "POST",
        "backend_domain": DOMAIN,
        "api_purpose": "修改角色权限",
        "auth_required": "yes",
        "auth_type": "JWT",
        "test_status": "untested",
        "source_file": SOURCE_FILE,
        "sample_body": None,
        "content_type": "application/json"
    },
    {
        "project_id": PROJECT_ID,
        "group_id": GROUP_ID,
        "api_path": "/permissions/authorizations",
        "api_method": "DELETE",
        "backend_domain": DOMAIN,
        "api_purpose": "删除权限授权",
        "auth_required": "yes",
        "auth_type": "JWT",
        "test_status": "safe",
        "source_file": SOURCE_FILE,
        "sample_body": None,
        "content_type": None
    }
]

# Credentials: both JWTs expired, skip
credentials = []

# Internal vulnerability
internal_vuln = {
    "title": "水平越权：伪造 organization-code 头跨组织访问数据",
    "type": "越权访问",
    "severity": "中危",
    "description": "低权限用户通过伪造 organization-code 请求头，可跨组织访问 authority-manage-service 的敏感数据，包括264个租户权限配置、4,696名员工PII（姓名/部门/头像）、165个部门信息、39个租户列表、14条合作伙伴信息（含手机号）、33个一级部门组织架构树、以及高权限用户在13个租户的角色分配信息。",
    "prerequisites": "1. 拥有 authority-manage-service 的有效低权限JWT；2. 伪造目标组织的 organization-code 请求头（如 DT001）。",
    "impact": "可获取其他组织的租户权限配置、员工PII、组织架构、部门信息、合作伙伴手机号、用户角色分配等敏感数据，造成大规模跨组织信息泄露。",
    "evidence": [
        "GET /permissions/tenants 返回全部264租户权限配置（12,848条权限记录）",
        "GET /token/tenants 返回DT001组织39个租户列表",
        "POST /organization/tenants/queries 返回DT001组织4条租户信息",
        "POST /v3/organization/staffs/queries 返回DT001组织4,696名员工姓名+部门+头像",
        "GET /v3/organization/chart 返回DT001完整组织架构树（33个一级部门）",
        "POST /departments/queries 返回DT001组织165条部门信息",
        "POST /v3/organization/partners/queries 返回DT001组织14条合作伙伴信息（含手机号）",
        "GET /user/{id}/power 返回高权限用户在13个租户的角色分配（含super_admin）"
    ],
    "reproduction_steps": [
        "获取低权限用户的JWT（issuer=911649184772145152，用户0319/hexin-sectest，org-code: TGosRaFhvb）",
        "对 authority-manage-service.saas-test.cn-beijing.volce.teamshare.work 发送请求，携带低权限JWT和伪造的 organization-code: DT001 请求头",
        "访问 /permissions/tenants、/v3/organization/staffs/queries、/v3/organization/chart 等端点，可获取DT001组织的敏感数据"
    ],
    "status": "confirmed",
    "source": "explore",
    "reported_by": "maintainer"
}

external_vuln = {
    "vuln_name": "GET /permissions/tenants 越权访问",
    "vuln_type": "越权访问",
    "project": PROJECT_ID,
    "severity": "中危",
    "source": "cairn",
    "curl_command": "curl -s -H 'Authorization: Bearer <LOW_PRIV_JWT>' -H 'organization-code: DT001' https://authority-manage-service.saas-test.cn-beijing.volce.teamshare.work/permissions/tenants",
    "description": {
        "danger": "低权限用户通过伪造 organization-code 请求头可跨组织访问数据，包括264个租户权限配置、4,696名员工PII、165个部门信息等敏感数据，造成大规模信息泄露。",
        "premise": "拥有 authority-manage-service 的有效低权限JWT，并能伪造目标组织的 organization-code 请求头。",
        "reproduce": "使用低权限JWT（issuer=911649184772145152）并添加 organization-code: DT001 请求头，访问 /permissions/tenants 等端点即可获取其他组织数据。",
        "fix": "在后端严格校验 organization-code 与JWT中声明的组织是否一致，禁止前端传入的 organization-code 覆盖后端鉴权结果。"
    }
}

results = {"assets": [], "credentials": [], "internal_vuln": None, "external_vuln": None}

# Post assets
for i, asset in enumerate(assets):
    status, body = post(f"{BASE}/assets", asset)
    print(f"Asset {i+1}/{len(assets)} ({asset['api_method']} {asset['api_path']}): {status}")
    results["assets"].append({"status": status, "body": body})

# Post credentials (none to post)
results["credentials"] = []

# Post internal vulnerability
status, body = post(f"{BASE}/projects/{PROJECT_ID}/vulns", internal_vuln)
print(f"Internal vuln: {status}")
results["internal_vuln"] = {"status": status, "body": body}

# Post external vulnerability
status, body = post(
    f"{MOZZIE}/api/public/vulnerabilities",
    external_vuln,
    headers={"Authorization": f"Bearer {MOZZIE_TOKEN}"}
)
print(f"External vuln: {status}")
results["external_vuln"] = {"status": status, "body": body}

print("\n--- Summary ---")
print(f"Assets posted: {len(assets)}")
print(f"Credentials posted: {len(credentials)}")
print(f"Vulns reported: 1")
print(json.dumps(results, ensure_ascii=False, indent=2))
