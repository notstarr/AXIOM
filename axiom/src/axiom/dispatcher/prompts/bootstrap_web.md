# Bootstrap 子提示词：Web／API

本文件补充 Web／API 引导工作流，通用规则以 `bootstrap.md` 为准。

## 目标绑定
目标类型填 `Web`。严格锁定当前 URL、域名与 API 入口；资产必须来源于当前页面、JS、Source Map、OpenAPI、响应或显式 Scope。

## Web 核心工作流（JS-first & 能力树）

### 1. 入口与资源获取
- 从入口收集 HTML、JS、动态 chunk、Source Map 及配置接口。SPA 统一 HTML 回退不得误记为 API。
- 登录态可用时，访问认证后控制台/后台，补全登录后 JS 资源。

### 2. 抽取 API 能力树（核心产出）
对业务主 JS Bundle（通常 > 200KB）进行深度分析，禁止仅靠简单正则扫零散 URL：
1. **前缀域**：识别前缀常量（如 `["/api/v1", "/api/automation", "/api/console"]`），全部列出。
2. **底座拼装函数**：定位 URL 构造函数（如 `const Foo=e=>'/prefix/'+e+'/resource'`），记录参数槽。
3. **子树展开**：以拼装函数为锚搜索调用点，还原增删改查及触发子端点（`GET/POST/PUT/DELETE/copy/trigger`）。
4. **隐藏/无登录入口**：搜索外部链接、`curl` 模板、`?token=`、`invite/join`、webhook 等。
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
明确记录对象 ID 来源：
- URL / Session 中的自身租户/工作区 ID（基线）；
- 自身只读列表响应（`items[].id`）；
- 邀请/分享明文 Token；短 ID 枚举候选（进 `initial_leads.md`）。

### 4. 接口身份验证原则（禁止破坏性写入）
- 允许：对自身对象发只读 GET 确认路径与字段；对异租户 ID/不存在 ID 发 GET 标定鉴权矩阵（200/403/404）；对疑似无登录端点发空 GET/POST 标定状态码。
- 禁止：对他人对象执行 POST/PUT/DELETE/trigger/邀请等破坏性操作。
- 响应中若含 `appSecret`/`token` 等明文直接进 `initial_leads.md`（“读即泄露”）。

## 产物要求
- `asset_inventory.md`：必须含**API 前缀域列表**、**按域展开的能力树**、参数/ID 来源、本租户基线 ID、鉴权矩阵及 JS 收集状态。
- `initial_leads.md`：整树对象级鉴权可疑点、响应密钥回显、无登录 webhook、短 ID 枚举点（同底座保持一条线索）。
- `findings.md`：仅记录由可复现请求确认的漏洞；本阶段无漏洞时写“本阶段未确认漏洞”。
