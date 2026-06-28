# AXIOM Handoff — 2026-06-24

## 本次改动摘要

### 1. PI Worker 启用/禁用
- 前后端完整实现 worker 禁用功能
- `WorkerConfig` 新增 `disabled: bool = False`
- `dispatch_workers` 表新增 `disabled` 列（含迁移）
- `_select_worker()` 跳过 disabled worker
- 启动健康检查跳过 disabled worker
- 前端每个 worker 卡片有「已启用/已禁用」切换按钮，禁用时卡片 50% 透明度
- Toggle API: `PUT /settings/dispatch/workers/{name}/toggle` — 直接 DB UPDATE，不碰其他设置

### 2. 移除 dispatch YAML 写入
- `PUT /settings/dispatch` 不再写 YAML，只写 DB
- 去掉了 `read_dispatch_yaml`/`write_dispatch_yaml` 调用
- YAML 仅作为首次启动的 fallback，后续全走 DB

### 3. 底部时间轴优化
- 高度：外容器 `h-10` → `h-14`（56px），内滚动区 `h-7` → `h-10`（40px）
- 鼠标滚轮映射到水平滚动：`@wheel.prevent="e => { $refs.timelineBar.scrollLeft += e.deltaY; }"`
- 滚动条高度 3px → 4px

### 4. Toggle toast 修复
- 点击切换后 toast 消息显示反了 → 已修复

## 当前运行状态

### Workers
| Worker | 类型 | 并发 | 优先级 | 状态 |
|--------|------|------|--------|------|
| deepseekv4-pro | bootstrap/reason/explore/plan | 2 | 1 | 启用 |
| glm-5.2 | bootstrap/reason/explore/plan | 4 | 1 | **禁用** |
| qwen3.6-plus | bootstrap/reason/explore/plan | 2 | 1 | 启用 |

### 配置
- Config version: 57
- Phase limits: bootstrap=1, explore=4, reason=1
- Explore timeout: 600s
- Server: http://127.0.0.1:8888
- DB: ~/.local/share/cairn/cairn.db

### proj_061 进度
- Facts: 75
- Vulns: 3
- Hints: 4
- Intents: 76 total, 73 concluded, 3 running
- Skill reads: 456

### 正在运行的 intent
- **i075** (deepseekv4-pro): 垂直越权创建含 DingMessage 节点的自动化流程
- **i076** (deepseekv4-pro): EOZB5148 组织 184 个参数深度读取分析
- **i077** (qwen3.6-plus): sinan-service datasets/model/associations/cards 端点测试

## 关键文件
- `cairn/src/axiom/dispatcher/config.py` — WorkerConfig + disabled
- `cairn/src/axiom/dispatcher/scheduler/loop.py` — 调度引擎，跳过 disabled
- `cairn/src/axiom/server/dispatch_config_store.py` — DB 读写 disabled
- `cairn/src/axiom/server/routers/settings.py` — API 端点，不再写 YAML
- `cairn/src/axiom/server/models.py` — DispatchWorkerSettings + disabled
- `cairn/src/axiom/server/db.py` — dispatch_workers 表 + disabled 迁移
- `cairn/src/axiom/dispatcher/runtime/startup_healthcheck.py` — 跳过 disabled
- `cairn/src/axiom/server/static/index.html` — 前端 worker 卡片 + toggle + 时间轴

## 启动命令
```bash
# 服务器
nohup uv run --project cairn axiom serve --host 127.0.0.1 --port 8888 --no-access-log --log-level info > /tmp/axiom-server.log 2>&1 &

# 调度器
nohup uv run --project cairn axiom dispatch --config dispatch.yaml --log-level info > /tmp/axiom-dispatcher.log 2>&1 &
```
