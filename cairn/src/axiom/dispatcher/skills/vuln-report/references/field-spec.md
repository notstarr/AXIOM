# 漏洞字段填写规范

## severity 评级标准

| 级别 | 标准 | 示例 |
|------|------|------|
| critical | 直接获取系统控制权、数据库全量数据、核心业务数据泄露 | RCE、SQL注入读取全量用户表、认证完全绕过 |
| high | 获取敏感数据、绕过关键安全控制、垂直/水平越权 | IDOR读取他人数据、任意文件读取、权限绕过 |
| medium | 需要特定条件触发、影响有限 | 存储型XSS、条件竞争、信息泄露 |
| low | 影响极小、需要特殊条件 | 反射型XSS(需交互)、低危信息泄露 |
| info | 安全配置观察、未验证的推测 | 缺失安全头、Cookie未设HttpOnly |

## status 状态说明

| 状态 | 含义 |
|------|------|
| pending | 已发现但未完全验证 |
| confirmed | 已验证，有完整证据链 |
| fixing | 已确认，正在修复 |
| fixed | 已修复 |
| false_positive | 误报 |

## evidence 写法

每条 evidence 必须是**自包含的**——单独看一条就能理解攻击过程：

```
✅ "【正常请求】curl -X GET https://target/api/users/123 → HTTP 200, {\"id\":123,\"name\":\"alice\"}"
✅ "【越权请求】curl -X GET https://target/api/users/124 -H \"Authorization: Bearer <alice_token>\" → HTTP 200, {\"id\":124,\"name\":\"bob\"} — 返回了其他用户数据"
❌ "越权成功"  — 太简略，没有请求和响应
❌ "见截图"    — 没有原始证据
```

## reproduction_steps 写法

步骤必须**可复制**——另一个测试者按步骤操作能复现漏洞：

```
1. 使用测试账号登录，获取 token
2. 访问 GET /api/users/123，确认返回自己的数据
3. 修改 ID 为 124，发送 GET /api/users/124，携带自己的 token
4. 观察返回了 ID=124 用户的数据（水平越权）
```
