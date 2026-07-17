---
name: credential-refresh
description: 当前凭证过期、接口返回 401/403 或需要重新登录时使用
---

# Credential refresh

1. 先运行 `axiom-tool credential list`，根据 label、auth_type、domains 选择候选凭证。
2. 仅在真正需要时运行 `axiom-tool credential get <credential_id>` 获取明文内容；不要把明文写入最终回答、Todo、Fact 或日志。
3. 用候选凭证最小化验证一个受保护请求。若返回 401/403/token expired，寻找同 Scope 内的登录/刷新接口，按正常业务流程获取新会话。
4. 当前 Explore CLI 不允许直接改写 Project Credential。把刷新后的凭证只保存在当前私有运行目录，并在 `leads` Artifact 中记录“需要人工更新 Project Credential”，只引用 Credential ID，不写明文。
5. 登录需要验证码/MFA 时不要绕过。记录阻塞原因，继续测试不依赖该凭证的方向。

刷新凭证是 Explore 的辅助步骤，不要为它创建新的 Intent 或 Todo，也不要中断原 Intent。
