# `duplicate` 审核附件示例

> 本文件展示如何证明“同根因重复”，而不是因为标题相似就判重复。

# 漏洞审核记录

## 1. 审核对象与原始声明

- 当前漏洞：`v018`——下载接口可跨租户读取发票 PDF
- 当前目标：`GET /api/files/invoice/{file_id}`
- 更早漏洞：`v006`——文件服务未校验租户归属，可读取其他租户附件
- 候选裁决：`duplicate --duplicate-of v006`

## 2. 已读取材料

- 当前 Finding/Evidence：`artifact:a210`、`artifact:a211`
- `v006` Finding/Evidence：`artifact:a055`、`artifact:a056`
- 两者均指向同一文件服务和同一个 `file_id` 授权中间件缺失。

## 3. 独立验证与关键对照

使用用户 A 请求租户 B 的测试发票文件：返回 `200 application/pdf`，PDF 水印显示 `tenant-b`。

对照：

- 用户 A 请求自己的发票：`200`；
- 无身份请求租户 B 发票：`401`；
- 用户 A 请求不存在文件：`404`。

随后复查 `v006` 的已验证范围：其目标明确包含 `/api/files/{category}/{file_id}`，根因是文件服务只检查登录态、不检查 `tenant_id`，影响对象包括 invoice 分类。当前结果没有发现新的授权边界或新的服务。

## 4. CVSS v4.0 风格维度核查

| 维度 | 当前漏洞 | `v006` | 比较结论 |
|---|---|---|---|
| AV | Network | Network | 相同 |
| AC | Low | Low | 相同 |
| AT | None | None | 相同 |
| PR | Low | Low | 相同 |
| UI | None | None | 相同 |
| VC | High | High | 同为跨租户文件泄露 |
| VI/VA | None/None | None/None | 相同 |
| SC/SI/SA | None | None | 相同 |

当前漏洞不单独生成新 Vector；沿用更早漏洞的已验证影响模型只是重复判断依据，不代表 Reviewer 修改严重等级。

## 5. 实际安全影响与边界

当前发票 PDF 是 `v006` 已覆盖文件类别的一个新样本，没有新增角色、服务、前提或影响类型。

## 6. 历史重复判断

满足重复条件：

- `v006` 创建更早、同 Task、未归档；
- 同一文件服务；
- 同一缺失租户归属校验根因；
- 同一身份边界和机密性影响；
- 当前 invoice 只是已覆盖类别中的新对象。

## 7. 裁决与理由

裁决：`duplicate --duplicate-of v006`。

```bash
axiom review decision submit duplicate --duplicate-of v006 --reason "已独立复现 invoice 文件跨租户读取；当前端点落入 v006 已验证的统一文件服务授权缺失范围，根因、身份边界和机密性影响相同，仅是新的文件类别样本。"
```

## 8. 不确定性及人工下一步

无影响重复判断的关键不确定性。若后续发现 invoice 由独立服务处理或可产生完整性影响，应创建新材料重新审核，而不是沿用本次重复裁决。
