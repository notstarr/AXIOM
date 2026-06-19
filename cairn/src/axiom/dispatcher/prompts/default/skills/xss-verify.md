---
name: xss-verify
description: 验证存储型/反射型 XSS 是否真实在受害者浏览器中执行。通过 CDP attach 到已登录受害者账号的 Chrome，导航到受害者视角页面，监听 alert dialog 事件确认 XSS 真实触发。
---

## 工具路径

```
repo-root/tools/xss-verify/detect.py    ← 主工具
repo-root/tools/xss-verify/launch.sh    ← 启动浏览器
```

依赖：`python3 + websocket-client`（已安装）

---

## 操作步骤

### Step 1：确保浏览器已运行

```bash
bash repo-root/tools/xss-verify/launch.sh
```

### Step 2：运行检测工具

```bash
python3 repo-root/tools/xss-verify/detect.py \
  "https://app.staging.cn-beijing.volce.target.work/victim-page" \
  --timeout 8 \
  --cdp-port 9223
```


### Step 3：判定结果

| 退出码 | 含义 |
|--------|------|
| `0` | dialog 触发，`dialog_message` 已输出，你自己核对是否与注入的 payload 匹配 |
| `1` | 未触发（检查页面 URL 是否正确、受害者视角是否能看到该字段） |
| `2` | Chrome 未运行，先执行 `launch.sh` |

---

## 证据模板

```
【注入】PATCH /api/v1/xxx (attacker JWT) body={"field":"<img src=x onerror=alert(1)>"} → HTTP 200
【检测命令】python3 detect.py "https://target/victim-page" --cdp-port 9223
【检测结果】exit 0 — dialog_message='1' ← 与注入的 alert(1) 匹配，XSS 真实执行确认
```

---

## 常见问题

**dialog 未弹出**：
- 确认是前端页面 URL，不是 API endpoint
- 确认受害者账号能看到该字段（用受害者 JWT curl 读取，验证 field 值含未转义 payload）
- 增加 `--timeout 15`

**多个 tab**：`--tab-index 1` 选择特定 tab

---