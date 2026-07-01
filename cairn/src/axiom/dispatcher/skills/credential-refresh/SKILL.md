---
name: credential-refresh
description: 任务需要凭证访问目标系统，或者当前凭证已过期时，使用该技能
---

## 前置：查询凭证库

当任务需要访问目标系统的受保护接口时，先从凭证库查询是否已有可用凭证：

```bash
python3 skills/credential-refresh/scripts/cred_store_inspect.py \
  --server "$AXIOM_SERVER" \
  list-creds --group "${GROUP_ID}"
```

- **有凭证且有效** → 直接从 `credentials_json` 中取出 token/cookie 使用，不需要刷新
- **凭证过期或无效**（遇到 401/403/token expired）→ 继续下面的刷新流程
- **无凭证** → 需要先通过登录获取凭证，也继续下面的流程

---

## 整体思路（凭证过期时）

```
查登录配置 → 构造登录请求 → 发包拿新凭证 → 写回凭证库 → 验证 → 继续任务
```

---

## 第一步：查询登录配置

凭证记录的 `asset_id` 字段指向登录接口资产。用该 ID 查询登录配置：

```bash
# 从凭证返回中拿到 asset_id（如 723），然后查询资产详情
python3 skills/credential-refresh/scripts/cred_store_inspect.py \
  --server "$AXIOM_SERVER" \
  show-asset-login --asset-id 723
```

**login_config_json 结构说明**：

| 字段 | 含义 | 示例 |
|------|------|------|
| `method` | 登录请求方法 | `POST` |
| `content_type` | 请求 Content-Type | `application/json` |
| `accounts` | 多权限账号字典 | `{"high_priv": {"phone": "135xxx", "password": "a123", ...}, "low_priv": {...}}` |
| `body_template` | 请求体模板，`<PHONE>`/`<PASSWORD>`/`<DEVICE_ID>` 为占位符 | 见下方 |
| `response_token_path` | 响应 JSON 中 token 的路径 | `data.authorization` |
| `response_headers` | 需要捕获的响应头 | `["set-cookie"]` |

**body_template 示例**：
```json
{
  "type": "phone_password",
  "phone": "<PHONE>",
  "password": "<PASSWORD>",
  "state_code": "+86",
  "device": { "id": "<DEVICE_ID>", "name": "Edge 149.0.0.0", "os": "Mac OS" }
}
```

---

## 第二步：构造并发送登录请求

根据 login_config 构造请求，替换占位符后发包：

```bash
# 示例：用 high_priv 账号登录
LOGIN_URL="https://<backend_domain><api_path>"
ACCOUNT_PHONE="13574105125"
ACCOUNT_PASSWORD="a12345678"
DEVICE_ID="0301548131e457b14f0b855461d8f467e2d20c778f3be4e1928a5f20ec6a7d66"

# 将 body_template 中的占位符替换为实际值，然后发请求
curl -s -X POST "$LOGIN_URL" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "phone_password",
    "phone": "'"$ACCOUNT_PHONE"'",
    "password": "'"$ACCOUNT_PASSWORD"'",
    "state_code": "+86",
    "device": {"id": "'"$DEVICE_ID"'", "name": "Edge 149.0.0.0", "os": "Mac OS", "os_version": "10.15.7"}
  }' -D /tmp/login_headers.txt -o /tmp/login_body.json

# 查看响应
cat /tmp/login_body.json | python3 -m json.tool
cat /tmp/login_headers.txt
```

**提取凭证**：
- 从响应 JSON 的 `response_token_path`（如 `data.authorization`）取出 token
- 从响应头 `Set-Cookie` 中取出 cookie
- 将所有鉴权 kv 对组装成 `credentials_json`

---

## 第三步：写回凭证库（先更新，后新增）

### 3.1 统一 label 命名规则（必须）

凭证 label 统一格式：

`{group}.{account}.{auth_type}.{env}`

示例：
- `pg_009.user0319.jwt.test`
- `pg_009.user5125.cookie.test`

规则：
- `group`：项目组 ID（如 `pg_009`）
- `account`：账号标识（用户名/手机号后四位/角色名）
- `auth_type`：`cookie|jwt|bearer|basic|custom_header|none`
- `env`：环境标识（如 `test|staging|prod`）

### 3.2 先查后更（命中同 label 必须 PUT）

```bash
python3 skills/credential-refresh/scripts/cred_store_inspect.py \
  --server "$AXIOM_SERVER" \
  list-labels --group "${GROUP_ID}"
```

**更新已有凭证（推荐，命中同 label 必须 PUT）**：

```bash
curl -s -X PUT "$AXIOM_SERVER/credentials/${CRED_ID}" \
  -H "Content-Type: application/json" \
  -d '{"credentials_json": "<JSON_STRING>", "domains_json": "<DOMAINS_JSON>", "source_task_id": "<INTENT_ID>", "active": true}'
```

**仅在同 label 不存在时新增**：

```bash
curl -s -X POST "$AXIOM_SERVER/credentials" \
  -H "Content-Type: application/json" \
  -d '{"group_id": "${GROUP_ID}", "label": "<LABEL>", "auth_type": "<TYPE>", "credentials_json": "<JSON_STRING>", "domains_json": "<DOMAINS_JSON>", "source_task_id": "<INTENT_ID>"}'
```

- `auth_type`：`jwt` / `cookie` / `bearer` / `basic` / `custom_header`
- `credentials_json` 是 JSON 字符串，存放所有鉴权 kv 对（token、cookie、Authorization header 等）
- `domains_json` 是 JSON 数组字符串，存放该凭证适用的域名列表（如 `["api.example.com"]`）
- `source_task_id` 建议填写当前 intent id，便于追踪
- POST 返回 409 = 同 group_id+label 已存在，必须改用 PUT；PUT 返回 404 = 已删除，改用 POST

---

## 方式 B（备选）：浏览器刷新

操控 Chrome-SecTest 重新登录目标系统，从浏览器中捕获新凭证。
适用场景：目标系统通过网页登录，或需要浏览器会话保持登录态。

---

## 写回后

1. 用新凭证发一次请求验证（期望 200）
2. 关闭浏览器 tab（如果打开了）
3. **立即继续原来的探索任务**，不要中断
4. 刷新过程中的发现记入 findings

## 无法刷新时

登录需要验证码或 MFA → 记录 findings 标记需人工介入，跳过凭证刷新，继续其他任务。
