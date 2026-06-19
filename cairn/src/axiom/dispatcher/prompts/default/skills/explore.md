---
name: explore
description: 当 Intent Description 包含"探索目标站点"或目标 URL 时使用此 skill。自动点击所有可达页面的按钮和链接，捕获 HTTP 流量，在 ai_dir 目录下产出 sitemap.yaml、api-list.yaml、api-page-map.yaml 三个文件。
---

## 任务说明

当你的 Intent Description 中包含"探索目标站点"、目标 URL、sitemap 或 API 清单等关键词时，按本 skill 的流程执行全站探索。

- **目标 URL**：从 Intent Description 中提取
- **输出目录**：使用上下文中的 `ai_dir` 路径（即三个 YAML 文件存放处）

---

## 前置条件

```bash
source ~/browser-use-env/bin/activate
```

工具路径：
- 流量查询：`python3 repo-root/tools/mitm/proxy_get_traffic.py`
- 浏览器：`browser-use --session {intent_id}`（用当前 intent_id 作为 session 名）

检查 mitmproxy 是否运行：
```bash
pgrep -f mitmdump && echo "✅ 运行中" || echo "❌ 未运行，流量无法捕获"
```

---

## 产出文件（存放在 ai_dir）

```
{ai_dir}/sitemap.yaml
{ai_dir}/api-list.yaml
{ai_dir}/api-page-map.yaml
```

---

## 执行流程

### Step 0：初始化

```bash
mkdir -p {ai_dir}
```

创建三个空 YAML（若不存在），然后清空历史流量：
```bash
python3 .../proxy_get_traffic.py --clear
```

截图留证：
```bash
browser-use --session {intent_id} open {target_url}
browser-use --session {intent_id} screenshot {ai_dir}/page_entry.png
```

### Step 1：探索循环（核心）

维护：
- **队列**：待访问的元素/链接
- **已访问集合**：防止重复

**每轮操作步骤：**

1. 清空上一轮流量：`python3 .../proxy_get_traffic.py --clear`
2. 执行操作（点击/导航）
3. 等待页面稳定，获取状态：`browser-use --session {intent_id} state`
4. 读取本次触发的 API：`python3 .../proxy_get_traffic.py --type api --summary`
5. 更新三个 YAML：
   - 新 URL → sitemap.yaml
   - 新 API 端点 → api-list.yaml（去重）
   - 当前页面 + 本次触发的 API → api-page-map.yaml
6. 将当前页面的新可交互元素加入队列

### Step 2：表单处理

遇到表单填虚假数据提交（`test_explore_001`、`test@explore.local`、数字 `1`），记录触发的 API。

### Step 3：危险操作

删除/重置类按钮：**优先操作自己创建的数据**，无法判断时跳过并记录。

### Step 4：结束条件

满足以下任一停止：
- 队列为空
- 连续 10 次无新 URL 或新 API
- 运行超过 30 分钟

停止前：`browser-use --session {intent_id} close`

---

## 范围控制

- **在范围内**：目标主域 + 附属子域名
- **跳过**：完全不相关的外部域名（记录但不跟进）

---

## YAML 格式规范

### sitemap.yaml
```yaml
target: "https://target.com"
pages:
  - path: /dashboard
    title: 控制台首页
    url: https://target.com/dashboard
    discovered_via: entry   # entry | click | navigation
    children: []
```

### api-list.yaml
```yaml
target: "https://target.com"
apis:
  - method: GET
    path: /api/v1/projects
    full_url: https://api.target.com/api/v1/projects
    status: 200
    content_type: application/json
    first_seen_on_page: /dashboard/projects
    response_preview: '{"code":1000,...}'
```

### api-page-map.yaml
```yaml
target: "https://target.com"
mappings:
  - page_url: https://target.com/dashboard/projects
    page_title: 项目列表
    trigger: page_load    # page_load | button_click | form_submit | navigation
    trigger_label: ""
    apis:
      - GET /api/v1/projects
      - GET /api/v1/user/me
```

---

## 注意事项

1. 每次操作前清空流量，保证 api-page-map 精准
2. YAML 增量写入，每轮循环后立即更新
3. 只记录流量中实际出现的端点，不推断
4. 每发现一个新页面截图一次，命名 `page_{short_path}.png`


## 任务说明

你是探索 worker。给定一个目标 URL，你需要：
1. 像真实用户一样浏览整个站点，点击所有可见的按钮、链接、菜单项
2. 边探索边用 mitmproxy 捕获触发的 HTTP 流量
3. 持续更新三个 YAML 产出文件

---

## 前置条件

```bash
source ~/browser-use-env/bin/activate
```

工具路径：
- 流量查询：`python3 repo-root/tools/mitm/proxy_get_traffic.py`
- 浏览器：`browser-use --session {intent_id}`

**mitmproxy 应已由系统在你启动前自动拉起。** 若不确定，检查：
```bash
pgrep -f mitmdump && echo "✅ 运行中" || echo "❌ 未运行"
```

---

## 产出文件

产出目录由任务上下文给出（`{output_dir}`），通常形如 `/tmp/axiom-files/pg_xxx/proj_xxx/explore/`。

三个文件：

```
{output_dir}/sitemap.yaml      # 站点页面树
{output_dir}/api-list.yaml     # 所有发现的 API 端点
{output_dir}/api-page-map.yaml # 页面 → 触发的 API 映射
```

---

## 执行流程

### Step 0：初始化

```bash
mkdir -p {output_dir}
```

创建三个空 YAML 文件（若不存在）：

**sitemap.yaml 初始内容：**
```yaml
target: "{target_url}"
pages: []
```

**api-list.yaml 初始内容：**
```yaml
target: "{target_url}"
apis: []
```

**api-page-map.yaml 初始内容：**
```yaml
target: "{target_url}"
mappings: []
```

清空历史流量记录：
```bash
python3 .../proxy_get_traffic.py --file /tmp/proxy_flows.jsonl --clear
```

### Step 1：启动浏览器，访问目标 URL

```bash
browser-use --session {intent_id} open {target_url}
browser-use --session {intent_id} state
```

截图留证：
```bash
browser-use --session {intent_id} screenshot {output_dir}/page_entry.png
```

把入口页面加入 sitemap.yaml，格式见下方「YAML 格式规范」。

### Step 2：探索循环（核心）

维护两个列表：
- **待访问队列**：`queue = [当前页面的所有可点击元素 + 链接]`
- **已访问集合**：`visited_urls = {入口 URL}`

**每一轮循环：**

1. **清空上一轮流量记录：**
   ```bash
   python3 .../proxy_get_traffic.py --clear
   ```

2. **取队列中下一个元素，执行操作（点击/导航）**

3. **等待页面稳定（约 1~2s），获取当前状态：**
   ```bash
   browser-use --session {intent_id} state
   ```

4. **读取本次操作触发的流量：**
   ```bash
   python3 .../proxy_get_traffic.py --type api --summary
   ```

5. **更新三个 YAML：**
   - 若 URL 发生跳转且是新页面 → 加入 sitemap.yaml，加入待访问队列
   - 读取到的 API 端点 → 加入 api-list.yaml（去重）
   - 记录「当前页面 → 本次触发的 API 列表」→ 加入 api-page-map.yaml

6. **当前页面的所有新可交互元素加入队列**

7. **重复直到队列为空或达到超时**

### Step 3：处理表单

遇到表单（登录框、搜索框、创建/编辑类表单）时：
- 填入虚假数据（姓名用 `test_explore_001`、邮箱用 `test@explore.local`、数字用 `1`、文本用 `test`）
- 点击提交按钮
- 记录表单提交触发的 API

**危险操作识别**：按钮文字包含「删除」「重置」「清空」「注销」等字样时，**优先操作自己创建的数据**，若无法判断是否是自己的数据，跳过并记录。

### Step 4：结束条件

满足以下任一条件时停止探索：
- 队列为空（所有可达元素已探索）
- 连续 10 次操作没有发现新 URL 或新 API
- 运行超过 30 分钟

停止前执行：
```bash
browser-use --session {intent_id} close
```

---

## 范围控制

- **在范围内**：目标主域名 + 其附属子域名（如 `api.target.com`、`auth.target.com`）
- **跳过**：跳转到完全不相关的外部域名时，记录该链接但不跟进

判断是否同域：检查域名后缀是否包含目标主域，如目标是 `target.work`，则 `saas.target.work` 在范围内，`github.com` 不在。

---

## YAML 格式规范

### sitemap.yaml

```yaml
target: "https://target.com"
pages:
  - path: /dashboard
    title: 控制台首页
    url: https://target.com/dashboard
    discovered_via: entry          # entry | click | navigation
    children:
      - path: /dashboard/projects
        title: 项目列表
        url: https://target.com/dashboard/projects
        discovered_via: click
```

### api-list.yaml

```yaml
target: "https://target.com"
apis:
  - method: GET
    path: /api/v1/projects
    full_url: https://api.target.com/api/v1/projects
    status: 200
    content_type: application/json
    first_seen_on_page: /dashboard/projects
    response_preview: '{"code":1000,"data":[...]}'    # 前 200 字符
```

### api-page-map.yaml

```yaml
target: "https://target.com"
mappings:
  - page_url: https://target.com/dashboard/projects
    page_title: 项目列表
    trigger: page_load           # page_load | button_click | form_submit | navigation
    trigger_label: ""            # 点击的按钮文字或表单名称
    apis:
      - GET /api/v1/projects
      - GET /api/v1/user/me
  - page_url: https://target.com/dashboard/projects
    trigger: button_click
    trigger_label: 新建项目
    apis:
      - POST /api/v1/projects
```

---

## 注意事项

1. **每次操作前清空流量记录**，保证 api-page-map 映射的精准性
2. **截图留证**：每发现一个新页面截图一次，命名 `page_{short_path}.png`
3. **YAML 增量更新**：每轮循环后立即写入，不要等全部完成再写（防止中断丢失进度）
4. **去重**：api-list.yaml 中同 method + path 的端点只记录一次
5. **不要猜测 API**：只记录流量中实际出现的端点，不推断
