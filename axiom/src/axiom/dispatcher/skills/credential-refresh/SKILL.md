---
name: credential-refresh
description: 当前凭证过期、接口返回 401/403 或需要重新登录时使用
requirements:
  commands:
    - name: axiom
      args: [--help]
      contains: AXIOM modular runtime
---

# Credential refresh

项目凭证必须渐进读取。不要猜测凭证格式，也不要一次读取所有凭证明文。

1. 先运行 `axiom credential list`。该命令只返回当前 Project 下已启用凭证的名称和 ID。
2. 选择确实需要的凭证后，运行：

   ```bash
   axiom credential get <credential_id> --output "$AXIOM_RUN_DIR/private/credential-<credential_id>.md"
   ```

   私有文件包含名称、可选备注、可选刷新 ID 和凭证原文，权限为 `0600`。标准输出只返回凭证 ID 和文件路径。
3. 在请求脚本内部读取私有文件并使用凭证。不要 `cat`、echo 或打印凭证；不要把 JWT、Cookie、Authorization、API Key、账号密码、SSH 私钥或其他 Secret 放进命令参数。
4. 只有请求明确返回 401/403、token expired，且凭证文件提供了刷新 ID 时，才运行：

   ```bash
   axiom credential refresh <refresh_id> --output "$AXIOM_RUN_DIR/private/refresh-<refresh_id>.txt"
   ```

   刷新说明同样只写入 `0600` 私有文件，标准输出不包含说明正文。
5. 按用户提供的刷新说明在当前 Run Directory 内获得并使用新凭证。Worker 只有读取权限，不得尝试改写 Project Credential；成功刷新后，只记录“需要人工更新凭证 `<credential_id>`”，不要记录新旧明文。
6. 凭证或刷新说明不得复制到 Fact、Artifact、Todo、漏洞、命令参数或会话可见输出。用完后清理局部变量和不再需要的私有文件。
7. 登录需要验证码或 MFA 时不要绕过。记录阻塞原因，继续测试不依赖该凭证的方向。

Bootstrap、Reason、Explore 都可以在自己的活动 Lease 中使用上述流程。刷新凭证只是当前阶段的辅助动作，不要因此额外创建 Intent 或 Todo。
