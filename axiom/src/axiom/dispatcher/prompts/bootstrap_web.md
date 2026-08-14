# Bootstrap 子提示词：Web／API

本文件只补充 Web／API 工作流，所有通用边界、技能、产物与完成合同以主 `bootstrap.md` 为准。

## 目标身份绑定

目标类型填写 `Web`。锁定一个网站、域名、URL 或 API 入口；资产必须由当前入口、跳转、页面/JS/Source Map/OpenAPI、响应、证书、任务材料或显式 Scope 直接关联。相似域名、搜索结果、其它 Host 和本机历史项目不得因名称接近而并入目标。

## Web 工作流

按顺序执行。SPA / 控制台 / B 端平台必须把第 3–6 步做完，不能在第 1 步“首页能打开”后收口。

### 1. 入口与资源

从未登录入口收集 HTML、Script、预加载资源、入口 JS、动态 chunk、Source Map、Service Worker、WebSocket 和配置响应，记录 URL、状态及证据路径。大量路径返回统一 SPA `index.html` 时，用标题或固定标记过滤，禁止把 HTML 回退记成 API。

### 2. 递归补全模块

沿 `import`、`require`、路由表、chunk 名称、资源前缀和运行时加载函数递归补全当前目标模块；第三方 SDK 只记录边界，不无限展开。业务主包（通常 > 200KB、含大量 `/api/` 字面量）必须下载后提取，不得只靠在线预览或截断输出。

页面、JS 或 Hint 指向登录后控制台、个人中心、订单、投诉、申诉、审批等路由时，使用可用凭证访问认证后资源并合并去重；覆盖受阻时明确记录，不得用未登录结果代替。

### 3. 从 bundle 抽出 API 能力树（Web 的核心产出）

不要停在“正则扫到若干 `/api/xxx`”。对每个业务主包按下面清单提取，写入资产清单：

1. **前缀域**：找出类似 `["/api/v1/...","/api/automation/","/api/console/...",...]` 的数组或常量。每个前缀单独成域。只展开 `/api/v1`、丢掉并列的 `/api/automation` 或 `/api/console`，视为提取失败。
2. **拼装函数**：定位 `` const Foo=e=>`/prefix/workspaces/${e}/resource` `` 或等价字符串拼接。记录函数名（minify 后的短名即可）、参数槽、返回模板。
3. **子树展开**：以该函数名为锚，在同一文件搜全部调用。把

   ```text
   Foo(e)
   `${Foo(e)}/${t}`
   `${Foo(e)}/${t}/copy|enable|disable|trigger`
   `${Foo(e)}/executions`
   `${Foo(e)}/agents/channel-bound`
   ```

   全部落成端点。`method` 以调用处的 `Fe(url,{method:"POST"|PUT|DELETE})` 为准；省略 method 记 GET。
4. **隐藏入口**：搜索前端自己拼的外部 URL、`curl -` 文案、`X-*-Token` 头名、`?token=`、`invite`/`join`、预签名 Host。这些经常是无登录 webhook / 邀请加入 / 文件下载。
5. **鉴权头最小集**：哪些头缺了前端直接判未登录。后面对自己的只读接口做一次“只 Cookie / Cookie+业务头”对照，确认真正门禁，避免 Explore 把 CSRF 防护误当成对象级鉴权。

输出最小单元示例（资产清单必须达到这个粒度，值换成当前目标的真实内容）：

```text
域: /api/automation/
底座: Yi(e) = /api/automation/workspaces/${e}/jobs
参数: e ← 路由 workspaceId / store；t ← 列表 items[].id
子路径:
  GET    Yi(e)
  GET    Yi(e)/{t}
  POST   Yi(e)
  PUT    Yi(e)/{t}
  DELETE Yi(e)/{t}
  POST   Yi(e)/{t}/copy|enable|disable|trigger
  GET    Yi(e)/executions?jobId=
  GET    Yi(e)/agents/channel-bound
鉴权提示: 自定义头 X-Business-Type 参与登录判断
展开状态: 已在主包搜完 Yi(
```

### 4. 参数从哪里来（禁止让后续“自己猜 ID”）

从 JS、Source Map 和真实响应提取页面/路由、API Host/路径、方法、参数、对象 ID、认证状态、角色判断、业务动作、状态值、上传下载、导出、GraphQL、OpenAPI 和 WebSocket 事件，建立“JS → 页面/模块 → API/业务动作”索引。

对象 ID 按优先级填写来源：

1. Origin / Referer / 当前 URL（自己的 workspace、session）。
2. 自己的只读列表响应 `items[].id`（jobId、agentId、webhookId、fileId）。
3. 邀请、分享、部署文档里的明文 token。
4. 短数字或固定前缀的枚举候选——只进 `initial_leads.md`，并写清“未验证是否存在”。

Explore 阶段应能直接抄这些来源去换 ID，而不是重新做信息收集。

### 5. 用会话确认接口身份，不在本阶段打写入

使用当前会话或可用代理流量确认接口与业务身份；静态字符串、普通 `200`、版本号、公开路径和理论配置风险只作为候选。

允许的确认：

- 对自己的对象发 GET/HEAD，确认路径真实、字段形态（是否回显 token、secret、成员列表）。
- 对 1 个异租户或 1 个不存在 id 发 GET，标定 403/404/200，写入鉴权矩阵。
- 对疑似无登录 URL 发不带 Cookie 的 GET/空 POST，只看状态码。

禁止：对他人对象 POST/PUT/DELETE/trigger/enable；禁止为了“看字段”去创建生产任务、邀请成员、触发 webhook。字段形态用自己的对象就够。

若只读响应里已经出现 `appSecret`/`botSecret`/`rawBotInfo`/`token` 明文，记入 `initial_leads.md` 并标“读即泄露，待 Explore 换对象验证”，不要在 Bootstrap 把密钥抄进资产清单正文以外的地方扩散。

### 6. 其它资产放最后

JS-first 后再补充 Host、端口、服务、WAF/网关指纹和其它 Web 资产，不得以目录猜测替代有证据的资源递归。指纹、缺安全头、版本号单独不成漏洞，最多进线索。

## 产物补充字段

- 资产清单必须包含：当前入口、关联 Host、认证前后页面与 chunk、**API 前缀域列表**、**按域展开的能力树**（含拼装函数与子路径）、参数槽与 ID 来源、本租户基线 ID、鉴权矩阵、菜单可见 / 代码可达 / 无登录三类计数、JS 收集状态。
- 初始线索优先写：整树可能缺对象级鉴权、列表响应疑似含密钥、无登录 webhook/join/预签名、短 ID 可枚举、角色码可伪造。同一底座的读写触发保持一条线索，方便 Reason 派成一个 Intent 而不是一堆碎片 GET。
- 已确认问题必须绑定真实请求、运行时行为、完整调用链或等价证据，并额外写明认证前后 JS 覆盖状态。Bootstrap 没有写入型验证时，不要把“接口存在”写成漏洞。
