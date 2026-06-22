# Crawler 修复与重构报告

> 日期: 2026-06-19  
> 涉及项目: `xrayAI/cairn` 爬虫模块  
> 状态: 已跑通，9 轮 crawl 全部成功，产出 18 页面 / 57 API / 20 映射

---

## 一、修了什么 Bug

### Bug 1: browser-use 会话冲突（致命）

**现象**: agent 每次执行 browser-use 命令，几乎都返回：

```
Session 'crawler-pg_006' is already running with different config.
Run `browser-use --session crawler-pg_006 close` first.
```

agent 被迫把每条命令包成 `pkill; close; sleep; retry`，仍然频繁失败。偶有 `Unauthorized`、`Could not discover a running Chrome instance`。

**根因**: browser-use 的 daemon/session 管理层在当前环境下不稳定。每次 CLI 调用会 spawn 一个新进程尝试连接/创建 daemon，多个进程竞争同一个 session 名，配置不一致就冲突。而底层 CDP 通道（`curl http://localhost:9223/json/list`）始终正常。

**后果**: `crawl` 阶段几乎从不产出有效 payload → 退化到 `flush` fallback → 只能被动吃 traffic 兜底，粒度粗、丢失业务语义。5 轮 crawl 全部失败，round 永远卡在 1。

**修复**: 彻底移除 browser-use，用直连 CDP 的无状态 helper 替换（见下文）。

---

### Bug 2: flush 阶段 `browser-use close` 误杀真实浏览器页面

**现象**: flush 阶段开始前，`_run_phase_once` 会执行：

```python
subprocess.run([
    "/bin/bash", "-lc",
    f"source ~/browser-use-env/bin/activate && browser-use --session {session_id} close ..."
])
```

这个 `close` 不仅关 browser-use session，还会把 Chrome 里**真实爬取中的页面**关成 `about:blank`，导致下一轮 crawl 从空白页开始。

**修复**: 删除整段 browser-use close 逻辑。新方案用专属 tab，由 supervisor 统一管理生命周期。

---

### Bug 3: JS 弹窗阻塞所有浏览器操作

**现象**: 页面上的 `alert()` / `confirm()` / `beforeunload` 弹窗会阻塞整个 CDP `Runtime.evaluate`，导致 `state` / `eval` / `click` 全部超时无响应。之前 agent 遇到这个就彻底卡死。

**修复**: CDP helper 的 `CDPSession` 类自动处理 `Page.javaScriptDialogOpening` 事件，收到弹窗立即 auto-dismiss（`Page.handleJavaScriptDialog {accept: true}`），并在建连时注入 JS 覆盖 `window.alert/confirm/prompt/beforeunload`，双重保险。

---

### Bug 4: 复用卡死的旧 tab

**现象**: browser-use 留下的旧 tab 可能处于卡死状态（JS 执行永久阻塞），新 crawl 复用这些 tab 时所有操作都超时。

**修复**: 爬虫不再复用任意 tab。supervisor 启动时用 `cdp_ctl new-tab` 创建专属 tab，每轮复用这个 tab，结束时 `close-tab` 清理。`start_crawler` 还会先关闭上一轮遗留的孤儿 tab。

---

### Bug 5: 前端无自动轮询，爬虫状态不刷新

**现象**: 前端的 `startPolling()`（5 秒间隔）只刷新 projects/tasks/events，不调用 `loadCrawlerStatus()`。爬虫启动后状态永远不更新，必须手动点"刷新状态"。

**修复**: 在 `startPolling` 循环中增加条件分支——当 `activePage === 'projects'` 且 crawler 正在 running/stopping 时，自动调用 `loadCrawlerStatus()`，同时如果结果 modal 打开着则实时刷新结果数据。

---

### Bug 6: 前端只有 YAML 预览，无法直观查看结构化结果

**现象**: 查看爬虫结果只能点"查看站点地图/API清单/页面映射"，弹出一个 YAML 纯文本 modal。对非技术人员不友好，且需要前端解析 YAML。

**修复**: 
- 后端新增 `GET /project-groups/{group_id}/crawler/results` 接口，直接返回 DB 中的结构化 JSON。
- 前端新增结构化结果 Modal，三个 Tab（页面卡片 / API 表格 / 映射卡片），带彩色标签、实时计数。

---

## 二、新增 / 修改的文件

### 1. `skills/explore/scripts/cdp_ctl.py` （新建）

无状态 Chrome DevTools Protocol CLI。核心设计：

- **无 daemon、无 session、无持久状态**。每次调用：连 Chrome → 执行一个动作 → 退出。从架构上消灭了 "Session already running" 冲突类。
- **`CDPSession` 类**：单次命令内开一个 WebSocket，自动 dismiss JS 弹窗，抗 recv 超时。
- **专属 tab 模型**：`new-tab <url>` 创建 tab 并等待加载完成；`--tab-id <id>` 直接操作指定 tab；`close-tab <id>` 清理。
- **命令集**：`state`（URL/title/readyState + 带索引的可交互元素）、`links`、`url`、`title`、`text`、`eval`、`click <idx>`、`click-text`、`click-href`、`open`、`screenshot`、`new-tab`、`close-tab`。
- 用项目 venv 的 python 运行（`python3`，依赖 `websocket-client`）。

### 2. `cairn/src/axiom/server/routers/crawler.py` （修改）

- **新增常量**：`_CDP_PY`、`_CDP_CTL`、`_CDP_ENDPOINT`。
- **新增 helper 函数**：
  - `_cdp_ctl(*args)` — 调用 cdp_ctl.py，返回 (returncode, output)。
  - `_create_crawler_tab(target_url)` — 创建专属 tab，返回 target id。
  - `_close_crawler_tab(tab_id)` — 关闭 tab。
  - `_ensure_crawler_tab(meta)` — 确保 tab 存活，不存在则创建，持久化到 meta。
- **`_run_crawler_supervisor` 改造**：
  - 启动时 `_ensure_crawler_tab` 创建专属 tab。
  - 每轮 crawl 前 re-confirm tab 存活。
  - `finally` 块中 `_close_crawler_tab` 清理。
- **`_run_phase_once` 改造**：
  - prompt 的 `{intent_id}` 改为传 CDP tab id（而非旧的 browser-use session 名）。
  - 删除 flush 阶段的 `browser-use --session ... close` 逻辑。
- **`start_crawler` 改造**：启动前关闭上一轮遗留的孤儿 tab；meta 初始化 `cdp_tab_id: ""`。
- **新增接口** `GET /project-groups/{group_id}/crawler/results`：直接从 DB 返回结构化 JSON（pages/apis/mappings 数组 + counts）。

### 3. `cairn/src/axiom/dispatcher/prompts/other/CrawlerExecute.md` （重写）

- 指示 agent 使用 `cdp_ctl.py --tab-id {intent_id}` 驱动浏览器。
- 明确禁止 `browser-use`。
- 删除所有 `pkill`/`close`/retry 稳定性噪声。
- 增加"如何驱动浏览器"操作指南（state → click-text/click-href → 确认 → 查 traffic）。

### 4. `cairn/src/axiom/dispatcher/prompts/other/CrawlerFlush.md` （重写）

- 同样禁用 browser-use，仅允许 `cdp_ctl url`/`state` 确认浏览器落点。
- 强调 flush 不做新探索，只用已有证据恢复增量。

### 5. `cairn/src/axiom/server/static/index.html` （修改）

- **新增 state 变量**：`crawlerResultsOpen`、`crawlerResultsTab`、`crawlerResultsData`、`crawlerResultsLoading`。
- **新增方法**：`viewCrawlerResults()`（打开 modal 并拉取数据）、`refreshCrawlerResults()`（刷新数据）。
- **`startPolling` 改造**：爬虫 running/stopping 时自动轮询状态 + 实时刷新结果 modal。
- **爬虫区域 UI 改造**：新增醒目的"查看爬虫结果"按钮，原 YAML 按钮保留为次级。
- **新增 Crawler Results Modal**：三 Tab 结构化展示（页面卡片 / API 表格 / 映射卡片），带彩色标签和实时计数。

---

## 三、架构变化对比

### 之前

```
agent → browser-use CLI → browser-use daemon → CDP → Chrome
                ↑
         每次调用 spawn 新进程
         daemon/session 竞争冲突
         "Session already running with different config"
```

### 现在

```
agent → cdp_ctl.py (无状态) → WebSocket → CDP → Chrome
                ↑
         每次调用独立连接
         无 daemon、无 session、无冲突
         自动 dismiss JS 弹窗
         专属 tab 隔离
```

---

## 四、验证结果

### 对 `https://xz.aliyun.com/news`（先知社区）的实跑

| 轮次 | 状态 | pages | apis | mappings |
|------|------|-------|------|----------|
| R1 | accepted | 4 | 22 | 4 |
| R2 | accepted | 8 | 24 | 8 |
| R3 | accepted | 10 | 28 | 10 |
| R5 | accepted | 15 | 33 | 15 |
| R9 (最终) | done | 18 | 57 | 20 |

- **9 轮 crawl 全部成功**，`"accepted": true`，**零 flush fallback**。
- round 持续递增（旧的"round 1 必死"问题消失）。
- `cdp_ctl.py` 驱动浏览器**零冲突**，agent 不再浪费时间在工具稳定性上。

### 发现的真实业务数据

- **页面**: 先知社区首页(`/news`)、文章详情(`/news/92255`)、安全培训(`/course`)、技术图谱(`/pictrue`)等 18 个
- **API**: `GET /news`(200)、`GET /news/{id}/comments`(200)、`GET /groups`(200)、`GET /api/v2/files/{uuid}`(302)、`GET /v3/log/init` 等 57 个
- **映射**: 20 个页面↔API映射，含 on_load API 和用户操作→API链路

### 测试

```
uv run pytest tests/test_crawler_runtime.py tests/test_server_api.py
→ 18 passed
```

---

## 五、未改动 / 已知限制

1. **redhaze 靶场**（`*.redhaze.top`）在修复期间处于半宕机状态（TCP 通但 TLS 握手挂起），这是外部基础设施问题，非代码问题。代码已就绪，目标恢复即可跑。
2. legacy YAML-ingest/export 代码仍在，低优先级可进一步清理（state-backed 逻辑视图已可独立工作）。
3. `cdp_ctl.py` 的 `open` 命令在极端网络条件下 `Page.navigate` 可能无响应（已做 continue-to-poll 容错，不阻塞后续操作）。
