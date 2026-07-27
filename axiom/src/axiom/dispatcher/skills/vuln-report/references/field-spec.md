# 漏洞字段填写规范

## severity 评级标准

| 级别 | 标准 | 示例 |
|------|------|------|
| critical | 直接获取系统控制权、数据库全量数据、核心业务数据泄露 | RCE、SQL注入读取全量用户表、认证完全绕过 |
| high | 获取敏感数据、绕过关键安全控制、垂直/水平越权 | IDOR读取他人数据、任意文件读取、权限绕过 |
| medium | 需要特定条件触发、影响有限 | 存储型XSS、条件竞争、有限信息泄露 |
| low | 影响极小、需要特殊条件 | 反射型XSS、低危信息泄露 |
| info | 安全观察，不应伪装成已确认漏洞 | 缺失安全头、版本信息 |

## 文件职责

- `findings.md`：本轮已确认事实和漏洞叙述。
- `leads.md`：发现但尚未完成验证的方向。
- `evidence/**`：原始请求、响应、工具输出、截图和漏洞证明。

未确认内容只能进入 `leads.md`，不能调用漏洞上报命令。

## evidence-path 写法

每个上报漏洞至少提供一个真实、非空的 `evidence/**` 相对路径：

```text
✅ evidence/request-01.txt
✅ evidence/http/order-idor-response.txt
✅ evidence/proof-01.png
❌ /tmp/response.txt
❌ ../credentials.json
❌ findings.md
```

请求与响应尽量拆分保存，文件中保留目标、方法、必要请求头、状态码和关键响应内容。不要在 CLI 参数或漏洞数据库中重复粘贴长证据。

## 状态

Explore 上报后由服务端固定保存为“待验证”。“已验证 / 忽略 / 重复”只能由人工在漏洞管理中调整；选择“忽略”时必须填写原因。
