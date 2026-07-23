---
name: credential-refresh
description: 当前凭证过期、接口返回 401/403 或需要重新登录时使用
---

# Credential refresh

1. 先运行 `axiom-tool credential list`，根据 label、auth_type、domains 选择候选凭证。
2. 仅在真正需要时运行 `axiom-tool credential get <credential_id> --output "$AXIOM_RUN_DIR/credential-<credential_id>.json"`。命令标准输出只会显示字段索引，明文只写入权限为 0600 的私有文件。
3. 在同一个请求命令内部读取该 JSON 文件并设置局部变量，不要运行 `cat`、不要 echo/打印凭证、不要把凭证字面量放进命令参数。请求结束后 `unset` 变量；不再需要时删除该私有文件。
4. 用候选凭证最小化验证一个受保护请求。若返回 401/403/token expired，寻找同 Scope 内的登录/刷新接口，按正常业务流程获取新会话。
5. 当前 Explore CLI 不允许直接改写 Project Credential。把刷新后的凭证只保存在当前私有运行目录，并在 `leads` Artifact 中记录“需要人工更新 Project Credential”，只引用 Credential ID，不写明文。
6. 登录需要验证码/MFA 时不要绕过。记录阻塞原因，继续测试不依赖该凭证的方向。

刷新凭证是 Explore 的辅助步骤，不要为它创建新的 Intent 或 Todo，也不要中断原 Intent。任何 Fact、Artifact、最终 JSON、会话可见输出或命令文本中都不得出现完整 JWT、Cookie、Authorization、API Key、STS 或 Secret。
