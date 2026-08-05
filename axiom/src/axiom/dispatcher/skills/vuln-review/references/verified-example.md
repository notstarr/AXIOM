# `verified` 审核附件示例

> 本文件展示 `artifacts/review.md` 的质量和证据粒度。示例目标、身份和数据均为虚构内容，不得直接复制为真实裁决。

# 漏洞审核记录

## 1. 审核对象与原始声明

- 漏洞：`v021`——普通用户可读取其他租户的订单详情
- 目标：`GET https://target.example/api/orders/{order_id}`
- 原始声明：用户 A 将路径中的订单 ID 替换为租户 B 的订单 ID 后，可读取 B 的收件信息和金额。
- 候选裁决：`verified`

## 2. 已读取材料

- Finding：`artifact:a120`
- Evidence：`artifact:a121`（原始请求摘要）、`artifact:a122`（旧响应截图）
- 已读取 Task Hint、Knowledge、`whatisvul` 与漏洞列表。
- 旧 Evidence 仅用于形成验证假设，没有直接作为最终结论。

## 3. 独立验证与关键对照

验证时间：2026-08-04T12:00:00Z。

身份：

- 用户 A：租户 `tenant-a` 的普通成员；
- 用户 B：租户 `tenant-b` 的普通成员；
- 两个订单均为测试数据，无真实个人信息。

复现请求：

```http
GET /api/orders/order-b-test HTTP/1.1
Host: target.example
Authorization: Bearer <user-a-redacted>
```

结果：返回 `200`，响应包含 `tenant_id=tenant-b`、测试收件人和订单金额。服务端没有校验订单归属。

关键对照：

1. 用户 A 请求自己的 `order-a-test`：`200`，属于正常基线；
2. 用户 A 请求不存在的订单：`404`；
3. 无 Authorization 请求 `order-b-test`：`401`；
4. 用户 B 请求 `order-b-test`：`200`。

对照排除了“端点公开”“订单 ID 不存在”“旧缓存响应”等替代解释。脱敏请求/响应保存为 `artifacts/evidence/idor-order-cross-tenant.md`。

## 4. CVSS v4.0 风格维度核查

| 维度 | 结论 | 当前证据 |
|---|---|---|
| AV | Network | HTTPS API 可远程访问 |
| AC | Low | 同一请求稳定复现 3 次，无竞态 |
| AT | None | 不依赖特殊部署或业务状态 |
| PR | Low | 需要普通用户登录 |
| UI | None | 不需要受害者交互 |
| VC | High | 可读取跨租户订单和收件信息 |
| VI | None | 本轮只证明读取，未证明修改 |
| VA | None | 无可用性影响 |
| SC | None | 未证明影响其他安全域 |
| SI | None | 未证明影响其他安全域 |
| SA | None | 未证明影响其他安全域 |

参考 Vector（仅因所有 Base Metrics 都有证据）：

```text
CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:H/VI:N/VA:N/SC:N/SI:N/SA:N
```

## 5. 实际安全影响与边界

- 已证明：已登录普通用户能够跨租户读取测试订单详情；
- 未证明：订单修改、批量枚举、管理员数据或其他系统影响；
- 原报告中的“可接管任意订单”没有证据，不纳入审核结论。

## 6. 历史重复判断

检查所有更早未归档漏洞，未发现相同订单归属校验根因。`v009` 是用户资料接口越权，端点、对象和服务端授权逻辑不同，不判重复。

## 7. 裁决与理由

裁决：`verified`。

理由：本 ReviewRun 使用两名测试用户完成跨租户读取的独立复现，并通过本人订单、无身份、不存在订单和资源所有者四组对照确认服务端缺少对象归属校验。实际影响限定为跨租户订单机密性泄露。

提交命令示例：

```bash
axiom review decision submit verified --reason "独立复现用户A可读取租户B测试订单；四组对照排除公开端点、无身份访问和缓存假象，实际影响为跨租户订单机密性泄露。"
```

## 8. 不确定性及人工下一步

- 未进行批量枚举；
- 未验证写操作；
- 如需扩大影响范围，应由新的 Explore Intent 在独立授权下执行，而不是 Reviewer 继续扩展。
