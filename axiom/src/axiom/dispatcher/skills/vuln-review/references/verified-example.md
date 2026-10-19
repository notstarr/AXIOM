# `verified` 漏洞报告示例

> 本文件展示**漏洞报告**（`artifacts/vuln_report-<vuln_id>-<slug>.md`）的质量与证据粒度。示例目标、身份和数据均为虚构。
> 示例文件名：`vuln_report-v021-idor-cross-tenant-order.md`

# v021 · 普通用户可读取其他租户的订单详情

## 简述

`GET /api/orders/{order_id}` 仅按订单 ID 查询，未校验订单归属。租户 A 的普通成员把路径中的订单 ID 换成租户 B 的订单后，可读取 B 的收件信息与订单金额（对象级授权缺失，IDOR）。

## 利用前提

- 持有任意租户的**普通成员**账号（无需管理员权限）；
- 知道或能枚举目标订单 ID；
- 目标接口可达，且无额外风控阻断。

## 复现步骤

1. 以用户 A（租户 `tenant-a` 普通成员）登录，取得 `Authorization`；
2. 将路径中的订单 ID 替换为租户 B 的 `order-b-test`；
3. 发送请求：

```http
GET /api/orders/order-b-test HTTP/1.1
Host: target.example
Authorization: Bearer <user-a-redacted>
```

4. 观察返回 `200`，响应体含 `tenant_id=tenant-b`、测试收件人与订单金额。

**关键对照**（用于排除替代解释）：

| 请求 | 期望 | 实际 |
|---|---|---|
| A 请求自己的 `order-a-test` | 200 | 200（正常基线） |
| A 请求不存在的订单 | 404 | 404 |
| 无 `Authorization` 请求 `order-b-test` | 401 | 401 |
| **A 请求 `order-b-test`** | —— | **200 + tenant-b 数据（越权）** |

对照排除了「端点公开」「订单不存在」「旧缓存响应」等解释。原始请求/响应：`evidence/idor-order-cross-tenant.md`。

## 漏洞危害

同一 SaaS 内任意租户成员可跨租户读取订单详情（收件人姓名/地址/电话、订单金额与商品），可批量遍历订单 ID 造成**全量客户 PII 与交易数据泄露**，不依赖任何前置条件或用户交互。属于对象级授权缺失导致的越权读取。

## 修复建议

- 服务端查询订单时**强制附加归属条件**（`tenant_id = 当前会话租户`），不要只按主键查；
- 把对象归属校验下沉到统一的数据访问层/中间件，避免各端点各自实现导致遗漏；
- 对订单 ID 使用不可枚举标识（如 UUID），并补充跨租户越权的回归用例。

## 证据截图

- `evidence/idor-order-cross-tenant.md`（原始请求/响应）
- 截图以**相对路径**引用，例如：`![](evidence/order-cross-tenant.png)`

> 只保留能证明漏洞存在的关键证据（关键响应 / 关键页面 / 关键数据回显），每个漏洞 0-3 张。
