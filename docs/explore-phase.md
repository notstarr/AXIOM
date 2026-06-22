# 全站探索功能 — 实现说明

## 背景

在漏洞测试之前，需要先摸清目标站点的全貌：有哪些页面、有哪些 API、哪个页面触发了哪些 API。以前这些都靠人工抓包，现在由 AI worker 自动完成。

---

## 整体架构

```
用户在项目管理页填目标 URL → POST /explore/start → explore intent 入库
            ↓
    dispatcher 自动调度 pi worker（explore 类型任务）
            ↓
    pi worker 加载 explore skill，读取 intent description 中的目标 URL
            ↓
    browser-use 操作 Chrome-SecTest → 点击所有按钮/链接
    mitmproxy 实时截获流量 → 写入 /tmp/proxy_flows.jsonl
    proxy_get_traffic.py 让 AI 随时查询触发了哪些 API
            ↓
    AI 边操作边更新三个 YAML 文件（sitemap / api-list / api-page-map）
            ↓
    后续漏洞测试 worker 读取这三个 YAML 决定测什么
```

---

## 实现的五个部分

### 1. 基础设施（mitmproxy 环境）

| 内容 | 说明 |
|---|---|
| mitmproxy 12.2.3 | 安装在 `~/browser-use-env` venv |
| CA 证书信任 | `~/.mitmproxy/mitmproxy-ca-cert.pem` 已添加到系统钥匙串 |
| Chrome-SecTest 代理 | `launch.sh` 支持 `MITM_PROXY=1` 环境变量，启动时加 `--proxy-server=http://127.0.0.1:18080` |

**Chrome-SecTest 带代理启动方式：**
```bash
MITM_PROXY=1 bash skills/browser-control/scripts/launch-chrome.sh
```

---

### 2. 工具脚本

#### `skills/browser-control/scripts/mitm_addon.py`
mitmproxy addon，后台录流量到 JSONL 文件。

- 自动跳过静态资源（JS/CSS/图片/字体）
- TLS 透传（CDN 域名不解密，避免 CDN 403）
- 目标域名过滤（通过 `MITM_TARGET_HOST` 环境变量）

**启动方式（AI 需要录流量时）：**
```bash
PROXY_FLOW_FILE=/tmp/proxy_flows.jsonl \
MITM_TARGET_HOST=target.example.com \
mitmdump -s skills/browser-control/scripts/mitm_addon.py \
  --listen-port 18080 -q &
```

#### `skills/browser-control/scripts/proxy_get_traffic.py`
AI 调用此脚本查询录制的流量，支持：

```bash

# 清空（每次操作前）
python3 .../proxy_get_traffic.py --clear

# 查看触发的后端 API
python3 .../proxy_get_traffic.py --type api --summary

# 只看特定路径
python3 .../proxy_get_traffic.py --filter /api/v1/ --summary

# 完整响应体
python3 .../proxy_get_traffic.py --filter /api/v1/user --full --last 1

# 查看进程状态
python3 .../proxy_get_traffic.py --status
```

---

### 3. explore.md skill

文件：`cairn/src/axiom/dispatcher/skills/browser-control/SKILL.md`

告诉 pi worker 如何执行全站探索：

1. **初始化**：创建三个空 YAML，清空历史流量
2. **探索循环**：
   - 清空流量 → 执行操作（点击/导航）→ 读流量 → 更新 YAML → 加入新元素到队列
3. **表单处理**：填虚假数据（`test_explore_001`、`test@explore.local`）提交
4. **危险操作**：删除/重置类按钮优先操作自己的数据
5. **结束条件**：队列为空 / 连续 10 次无新发现 / 超过 30 分钟

---

### 4. 后端 API

#### `POST /projects/{project_id}/explore/start`
创建一个 explore intent，dispatcher 自动调度 worker 执行。

```json
// 请求体
{ "target_url": "https://target.example.com" }

// 响应
{
  "project_id": "proj_050",
  "intent_id": "i001",
  "status": "running",
  "target_url": "https://...",
  "output_dir": "/tmp/axiom-files/pg_003/proj_050/ai",
  "sitemap_exists": false,
  "api_list_exists": false,
  "api_page_map_exists": false
}
```

#### `GET /projects/{project_id}/explore`
查询探索状态：`idle` / `running` / `done`，以及三个 YAML 文件是否已生成。

---

### 5. 前端 UI

**位置**：项目管理页 → 选择项目 → 选择关联任务 → 滚动到底部 → 「全站探索」区块

**功能**：
- URL 输入框 + 「启动探索」按钮 → 调用 `POST /explore/start`
- 状态卡片：intent ID、worker 名、运行状态徽章
- 产出文件三格：`sitemap.yaml` / `api-list.yaml` / `api-page-map.yaml`，✓ 表示已生成
- 「刷新状态」按钮

---

## 三个产出文件格式

### sitemap.yaml — 站点页面树
```yaml
target: "https://target.com"
pages:
  - path: /dashboard
    title: 控制台首页
    url: https://target.com/dashboard
    discovered_via: entry   # entry | click | navigation
    children:
      - path: /dashboard/projects
        title: 项目列表
        url: https://target.com/dashboard/projects
        discovered_via: click
```

### api-list.yaml — 所有 API 端点
```yaml
target: "https://target.com"
apis:
  - method: GET
    path: /api/v1/projects
    full_url: https://api.target.com/api/v1/projects
    status: 200
    content_type: application/json
    first_seen_on_page: /dashboard/projects
    response_preview: '{"code":1000,"data":[...]}'
```

### api-page-map.yaml — 页面→API 映射
```yaml
target: "https://target.com"
mappings:
  - page_url: https://target.com/dashboard/projects
    page_title: 项目列表
    trigger: page_load        # page_load | button_click | form_submit
    trigger_label: ""
    apis:
      - GET /api/v1/projects
      - GET /api/v1/user/me
  - page_url: https://target.com/dashboard/projects
    trigger: button_click
    trigger_label: 新建项目
    apis:
      - POST /api/v1/projects
```

产出文件位置：`/tmp/axiom-files/{pg_id}/{proj_id}/ai/`

---

## 使用前提

1. **Chrome-SecTest 已登录目标站点**（手动登录一次，session 会保留）
2. **mitmproxy 在 18080 端口运行**（启动探索时由 skill 负责检查）
3. **Chrome-SecTest 以代理模式启动**：`MITM_PROXY=1 bash launch.sh`

---

## 文件清单

| 文件 | 说明 |
|---|---|
| `skills/browser-control/scripts/mitm_addon.py` | mitmproxy addon，录流量 |
| `skills/browser-control/scripts/proxy_get_traffic.py` | AI 查询流量的 CLI 工具 |
| `skills/browser-control/scripts/launch-chrome.sh` | 已更新，支持 `MITM_PROXY=1` |
| `cairn/src/axiom/dispatcher/skills/browser-control/SKILL.md` | explore skill |
| `cairn/src/axiom/server/models.py` | 新增 `ExploreStartRequest`、`ExploreStatus` |
| `cairn/src/axiom/server/routers/projects.py` | 新增两个 explore 路由 |
| `cairn/src/axiom/server/static/index.html` | 项目管理页新增「全站探索」区块 |
