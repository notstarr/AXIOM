# Bootstrap 子提示词：Web／API

本文件补充 Web／API 引导工作流，通用规则以 `bootstrap.md` 为准；主 `bootstrap.md` 为准。

## 目标绑定
目标类型填写 `Web`。目标身份必须明确记录并严格锁定当前 URL、域名与 API 入口；资产必须来源于当前页面、JS、Source Map、OpenAPI、响应或显式 Scope。

## Web 核心工作流（JS-first & 能力树）

### 1. 入口与资源获取
- 从入口收集 HTML、JS、动态 chunk、Source Map 及配置接口。SPA 统一 HTML 回退不得误记为 API。
- 登录态可用时，访问认证后资源（控制台/后台），补全登录后 JS 资源。

### 2. 抽取 API 能力树（核心产出）
**能力树数据底座（jsfinder，先跑）**：
   ```bash
   cd "<主提示词给出的运行目录>" && "$AXIOM_PYTHON" "$AXIOM_BOOTSTRAP_TOOLS/jsfinder.py" "<入口URL>"
   ```
   **默认参数即可跑**（除入口 URL 外都可选；需要登录态时加 `--cookie`）。读 `AXIOM_RUN_DIR`，产出十类信息（新增「前端路由」分类(Vue/React/Angular 路由声明与导航调用，按站点分组)，API接口按域/前缀分组，含 `METHOD path | query/body/header` 调用点注解；登录态可用时加 `--cookie "<会话Cookie>"` 补认证后接口）。退出码：0=有结果；2=零命中（换入口或带登录态重跑）；3=入口不可达（在资产清单中如实记录收集失败）。该 JSON 为可选数据底座；资产清单与初始线索清单仍由你基于它结合深度分析撰写。
   登录态到手后（`"$AXIOM_BSK" credentials --domain <域名> --session <id>` 取 Cookie/Storage，或登录页 Cookie），**必须带 Cookie 再跑一次**——认证后 chunk 里的接口树是匿名爬取拿不到的。这次运行产出的是**另一份独立产物**（不替换匿名轮那份），两份都要保留：
   ```bash
   cd "<运行目录>" && "$AXIOM_PYTHON" "$AXIOM_BOOTSTRAP_TOOLS/jsfinder.py" "<认证后页面URL>" --cookie "<会话Cookie>"
   ```
**产物「一次运行一个文件」，默认不覆盖**：名为 `jsfinder_result-<域名>-<路径>-<随机后缀>.json`；`--out` / `--tag` 可选，通常不用传。
   **每个入口各跑一次**：入口不止一个时（如 `/admin`、`/test`）分别跑、各留一份产物。**凡是在页面 / JS / Source Map 里发现的新前端路由或入口（其它路径、管理台、其它子域），都要用同一脚本补跑一次**，不要以为一轮扫描就覆盖了全站。若某次运行 JS 数撞到 `--max-js` 上限，说明**没扫全**，应缩窄入口或提高上限再跑。收口前请读**全部** `jsfinder_result*.json` 合并成能力树。
对业务主 JS Bundle（通常 > 200KB）进行深度分析，禁止仅靠简单正则扫零散 URL：
1. **前缀域**：识别前缀常量（如 `["/api/v1", "/api/automation", "/api/console"]`），全部列出。
2. **底座拼装函数**：定位 URL 构造函数（如 `const Foo=e=>'/prefix/'+e+'/resource'`），记录参数槽。
3. **子树展开**：以拼装函数为锚搜索调用点，还原增删改查及触发子端点（`GET/POST/PUT/DELETE/copy/trigger`）。
4. **隐藏/无登录入口**：搜索外部链接、相似域名、`curl` 模板、`?token=`、`invite/join`、webhook 等。
5. **鉴权头最小集**：分析前端判登依据（Cookie / 自定义 Token 头）。

*资产清单能力树标准格式示例：*
```text
域: /api/automation/
底座: Yi(e) = /api/automation/workspaces/${e}/jobs
参数: e ← 路由 workspaceId；t ← 列表 items[].id
子路径:
  GET    Yi(e)
  GET    Yi(e)/{t}
  POST   Yi(e)
  PUT    Yi(e)/{t}
  DELETE Yi(e)/{t}
  POST   Yi(e)/{t}/copy|enable|disable|trigger
鉴权提示: X-Business-Type 头参与鉴权
```

### 3. 参数与对象 ID 来源（禁止让后续猜 ID）

**先确认身份数量（多浏览器实例 = 多身份）**：运行 `"$AXIOM_BSK" browsers`。
- 仅一个在线实例：按下面的“本租户基线”记录一份即可。
- 多个在线实例：**逐个实例各取一份基线，不得只取一个就收口**。每个实例是独立 Cookie 沙箱（独立登录身份）：
  ```bash
  "$AXIOM_BSK" session start --no-focus --browser <instance-id>   # 记下返回的 4 字符 session id
  "$AXIOM_BSK" navigate <入口URL> --session <id>                  # 必须先打开目标页：credentials 只读“已打开页面”的 storage
  "$AXIOM_BSK" credentials --domain <入口域名> --session <id>     # 该身份的 Cookie/Storage，写入 evidence/
  "$AXIOM_BSK" session stop <id>
  ```
  对每个实例重复“start → **navigate** → 取证 → stop”；**取到就当场把该身份的组织/用户 ID 记进产物**，不要留到后面补——session 空闲过久会被自动回收，那时再读就取不到了。
- 取证后确认多个实例是**同一身份**（同一账号）时，注明“实例 A 与 B 同身份”即可，不必重复罗列；**未真正打开取证过的实例不得凭推断合并**。
- 实例打不开或取不到身份时，如实写“实例 `<id>` 未取证（原因）”，不得猜。

明确记录对象 ID 来源：
- URL / Session 中的自身租户/工作区 ID（基线）；
- 自身只读列表响应（`items[].id`）；
- 邀请/分享明文 Token；短 ID 枚举候选（进 `initial_leads.md`）。

### 4. 接口身份验证原则（禁止破坏性写入）
- 允许：对自身对象发只读 GET 确认路径与字段；对异租户 ID/不存在 ID 发 GET 标定鉴权矩阵（200/403/404）；对疑似无登录端点发空 GET/POST 标定状态码。
- 禁止：对他人对象执行 POST/PUT/DELETE/trigger/邀请等破坏性操作。
- 响应中若含 `appSecret`/`token` 等明文直接进 `initial_leads.md`（“读即泄露”）。

## 产物要求
- 能力树清单：必须含**API 前缀域列表**、**按域展开的能力树**、参数/ID 来源、本租户基线 ID、鉴权矩阵及 JS 收集状态,尽可能收集全；若未能收集全在要在文件中说明。存在多个身份时，基线必须按身份分节（实例 ID + 身份标识 + 该身份的租户/用户/工作区 ID + 取证来源）。
- 初始线索清单：整树对象级鉴权可疑点、响应密钥回显、无登录 webhook、短 ID 枚举点（同底座保持一条线索）。
