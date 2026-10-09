---
name: browser-skill
description: |
  使用 bsk 命令驱动用户的真实浏览器（已登录态）：访问页面、填表、点击、
  接口探测、抓取数据。要求本机 bsk CLI 与浏览器扩展已连接。
  本版本为 axiom 定制：凭据提取是渗透测试的合法产出；scope 外目标禁止访问。
  命令面与上游 bsk 0.3.2 对齐（网站调试用 debug；network 仅索引）。
---

# browser-skill（axiom 定制）

通过 `$AXIOM_BSK` 包装器驱动用户的真实 Chromium 浏览器。自动化在独立的
**Agent Window** 中进行，使用用户已有的登录态与 Cookie。用户自己的标签页
受保护——除非显式 borrow，用完必须归还。

**不要**将该 skill 用于：无浏览器场景、扩展安装、或用户只想要操作说明的情况。
**scope 边界**:Intent 的目标范围在**派发时**由 AXIOM 校验(声明即边界);浏览器工具
执行期不做域拦截,本 skill 的所有命令对任何目标照常可用。目标选择仍须遵守任务书
与 scope 约定——越界测试不会被工具拦截,但会出现在审计日志与 findings 审查里。
**凭证卫生**：不得从页面提取/外传与测试无关的凭据；Cookie/凭据数据只写
`evidence/` 目录（证据完整性优先），不要把凭据明文写进 findings/leads 正文。

## 必需生命周期

```text
1. "$AXIOM_BSK" session start [--browser <instance-id>]   # 记下输出的 4 字符 session id
2. "$AXIOM_BSK" <命令> --session <id>                     # 所有会话级命令都要带
3. "$AXIOM_BSK" session stop <id>                         # 成功与失败路径都必须执行
```

不要依赖空闲超时清理。目标达成即 stop。stop 会归还借用的标签。
任何命令会自动拉起所需后台服务；不要手工管理守护进程。
Agent Window 不需要打断用户时，session start 加 `--no-focus`。
启动/传输问题重试一次仍存在时运行 `bsk doctor`。

## 多浏览器实例（多身份）

本机可能同时在线多个浏览器实例（`bsk browsers` 查看）。每个实例是独立的
Cookie 沙箱 = **独立的登录身份**，登录态持久保存在实例内，session 重开不丢。

- 任务书指定了浏览器实例/身份时：`session start --browser <instance-id> --no-focus`，该 session 的全部操作发生在该身份的登录态下。
- 多实例在线且任务书未指定时，`session start` 会报 "more than one browser is online"——按任务书语义选择对应实例，不要盲选。
- 阶段或任务要求覆盖多个身份时（如 bootstrap 需要各身份基线）：对每个实例分别 `session start --browser <id>` → 取证/操作 → `session stop <id>`，逐个走完再收口；不得只用一个实例的登录态代表全部，也不得凭推断合并身份。
- 目标实例从 `bsk browsers` 列表消失 = 扩展休眠掉线：在该实例窗口点一下 BrowserSkill 扩展图标唤醒重连，再重试 start。
- session 存活期间心跳会保活扩展连接；同一身份的后续 intent 复用同一实例。

## 单一可观察目标

从任务请求推导具体成功条件。走最短路径：观察 → 操作 → 至多一次观察确认。
成功可见后不再点击/刷新/导航/换 tab。两次尝试无进展或遇到人工步骤 → request-help。

## 观察-操作-观察（默认循环）

```text
"$AXIOM_BSK" navigate <url> --session <id>
"$AXIOM_BSK" observe --session <id>
"$AXIOM_BSK" click|hover|fill|select|press ... --session <id>
"$AXIOM_BSK" observe --session <id>
```

优先用新鲜的 `@eN` ref 而非 CSS 选择器。导航会使 ref 失效；大 DOM 变化同样。
悬停才出现的菜单：observe 不主动悬停；当预期控件缺失且无标记指向触发器时，
用一次 `observe --probe-hover`；找到触发器后 `hover <ref>` 再 observe 再操作。
读取升级顺序：observe → probe-hover → snapshot → get-html → screenshot。
不要一上来就用原始 HTML 或截图找普通控件。

## 借用用户标签（登录态复用）

worker 需要用户已登录的页面时：

```bash
"$AXIOM_BSK" tab list --scope user --session <id>     # 找到目标标签 id
"$AXIOM_BSK" tab borrow <tab-id> --session <id>       # 借用（移入 Agent Window）
"$AXIOM_BSK" evaluate "..." --session <id>            # 在用户会话上下文执行
"$AXIOM_BSK" tab return <tab-id> --session <id>       # 用完立即归还
```

借用期间的操作以用户身份进行。用完立即归还，不要跨无关任务保持借用。

## 多标签与可见范围

Agent Window 内可以有多个标签；后台标签不用选中也能被操作：

```bash
"$AXIOM_BSK" tab create --url <url> --no-active --session <id>   # 后台开 tab，记下返回的 tab_id
"$AXIOM_BSK" observe --tab-id <tab_id> --session <id>            # 后续命令都带 --tab-id 指向它
"$AXIOM_BSK" navigate <url> --tab-id <tab_id> --session <id>
"$AXIOM_BSK" tab select <tab_id> --session <id>                  # 需要用户看见时才切到前台
```

`--tab-id` 可加在 observe / snapshot / get-html / screenshot / console / network /
navigate / click / fill / select / press / evaluate 等命令上；不加时默认作用于 Agent
Window 的当前活动标签。后台标签里的页面继续运行，viewport 与整页截图在后台也可用。
不要自己编 tab id，也不要跨无关任务一直占着用户的标签。

`tab list` 的可见范围——**判断"是否有别的 agent 在动这个浏览器"时必须知道**：

- `--scope user`：用户自己的标签（任何非 Agent Window 的窗口）；
- `--scope agent`：**本次查询所用 session** 的 Agent Window 内标签；
- `--scope all`（默认）：上面两者之和。

也就是说，用**自己**的 session 查 `--scope all` **看不到其他 session 的 Agent Window**。
这不是隔离保证：用对方的 session id 查 `tab list --session <other-id> --scope agent`
就能看到对方在开哪些页面（session id 从 `session list` 可得，不是秘密）。
同一浏览器实例共享 Cookie，所以"我这边 `tab list` 很干净"不等于没人并行在同一
登录态上操作——`session list` 里有几个 session，就有几个可能的并行动作者。

## 凭据提取（axiom 定制）

```bash
"$AXIOM_BSK" credentials --domain <domain> --session <id>
```

返回该域全部 Cookie（**含 HttpOnly**）+ 已打开页面的 localStorage/sessionStorage，
以及拼好的 `Cookie` 请求头。渗透测试中会话凭据是合法产出：用于认证后接口探测
（Authorization/Cookie 头）与对象级越权对照。**结果只写 evidence/ 目录**，
findings/leads 正文引用 evidence 路径，不要内联明文。

**域匹配是后缀精确的，且不含父域。** `--domain` 直接透传给
`chrome.cookies.getAll({ domain })`，只返回「域恰好等于该值」或「其**子域**」的
cookie；**设在父域上的会话 cookie 不会返回**：

```bash
"$AXIOM_BSK" credentials --domain wedata.example.com --session <id>   # 漏掉 .example.com 上的会话 cookie
"$AXIOM_BSK" credentials --domain example.com --session <id>          # 优先：传可注册父域，再按 cookie 名自筛
```

「只回了一个无关 cookie」通常就是这个原因，**不是 HttpOnly 限制**——
读 HttpOnly 正是本工具存在的意义（`chrome.cookies` 能读，页面 JS 不能），
结果里每个 cookie 都带 `http_only` 标记。**只有用父域重试后仍找不到会话
cookie，才可以判定会话不在 cookie 里**（可能在 localStorage，或属于另一个域）。

## 人工接管

撞验证码/登录/短信验证码/支付确认时：

```bash
"$AXIOM_BSK" request-help --session <id> --prompt "请在弹出的页面完成 domery.cn 登录"
```

outcome：`continued`/`completed` → 恢复控制，先 observe 再继续；
`cancelled` → 视为拒绝；`timed_out`/`disabled` → 记为阻塞上报，不要蛮力重试。

## 命令清单（封闭，不要发明清单外的命令）

```text
session start|stop|list   browsers   status   doctor   logs
navigate   navigate-back   navigate-forward   reload   wait-for-navigation   wait-ms
observe   snapshot   get-html   screenshot   console   network   debug
click   hover   wheel   scroll-to   focus   blur   fill   select   press   evaluate
tab list|create|close|select|borrow|return   window resize   emulate
upload   download   request-help   record start|stop
credentials --domain <domain> --session <id>   # axiom 定制
```

常用易错参数：

```text
"$AXIOM_BSK" fill <ref> --value <text>
"$AXIOM_BSK" select <ref> --value <option-value>
"$AXIOM_BSK" screenshot --full-page --out <path>          # 整页拼接(默认 --scope follow 跟随追加内容)
"$AXIOM_BSK" scroll-to <ref> --session <id>               # 滚动到指定元素(触发懒加载)
"$AXIOM_BSK" wheel --session <id>                         # 原生滚轮事件
"$AXIOM_BSK" click <ref> --capture <capture-id> --image-x <x> --image-y <y>
"$AXIOM_BSK" upload <ref> --file <path>
"$AXIOM_BSK" download <ref> --out <path>

- `network` 是 DevTools 面板的 worker 版，**只返回索引**：每条一行（method / url /
  status / mime_type / resource_type / 失败原因），游标式取增量——先记 `--since`
  游标（或用返回的 `next_since`）再触发功能，然后按游标读增量。默认 50 条，
  扩展上限 200；`--max-text-chars` 截断 URL 与错误文本。
  **索引不含请求头、请求体、响应体。**
- **需要请求头 / 请求体 / 响应体时用 `debug`**（上游网站调试能力；不要再指望
  `network` 给出正文——它只有索引）。网站调试必须在导航/复现**之前**开启采集：

  ```bash
  "$AXIOM_BSK" debug start --session <id> --name "<任务名>"
  "$AXIOM_BSK" navigate <url> --session <id>
  # …用常规命令操作页面…
  "$AXIOM_BSK" debug requests --session <id>                      # 请求列表(游标)
  "$AXIOM_BSK" debug request <request-id> --session <id> --part response
  "$AXIOM_BSK" debug request <request-id> --session <id> --part headers
  "$AXIOM_BSK" debug request <request-id> --session <id> --part response --pointer /error/code
  "$AXIOM_BSK" debug export --session <id> --output "<运行目录>/evidence/debug.json"
  "$AXIOM_BSK" debug stop --session <id>
  ```

  `--part` 取值 `metadata|request|response|headers|timing`；长正文用 `--offset` /
  `--max-chars` 分片，`--pointer` 按 RFC 6901 JSON pointer 直接取字段。
- `debug` 还提供 `aggregate`（慢请求）、`duplicates`（重复请求分析）、`performance`、
  `rules` / `replay`（请求规则与重放）、`console` / `pages` / `operations`
  （与操作关联的证据）。收口前 `debug stop` 结束采集；历史记录会保留，可 `debug export` 导出。
- `emulate` 对单个 tab 应用视口/UA/触屏覆盖；新 tab 不继承；`--off` 恢复。
- `scroll-to`/`wheel` 用于无限滚动与懒加载页：滚一段 → observe/network 看增量。
  优先走这两个原生输入路径；`wheel` 的 `--delta-x`/`--delta-y` 是输入量、
  **不保证滚动距离**，滚完必须 observe 确认真实结果。`evaluate window.scrollTo`
  只是退路：它同样会触发页面自己的 scroll 监听，但走的是 JS 捷径而非真实输入
  路径，虚拟列表一类组件可能只认真实输入；确有必要才用，并同样 observe 核实。
- `screenshot --full-page` 拼接整页证据。**默认 `--scope follow` 跟随追加内容**；
  `--scope current` 只截初始文档高度，之后追加的内容**全部丢弃**——只在「已加载
  区间本身就满足需求」时用它，且必须如实上报截取范围，不能声称把整页/整个 feed
  都截到了。`loading_stalled` 表示底部持续 30s 挂着 loading 指示且高度不增长，
  不要盲目加 `--timeout`；`--timeout` 只在整页模式生效（默认 2m），失败不留下半张图。
- Canvas 页面（图形验证码区域、绘图应用）：observe 返回 `@eN canvas [visual:screenshot]`
  文本引用。先 `screenshot --ref @eN` 看图，再用 `click --capture <capture-id>
  --image-x --image-y` 以**原图坐标**点击；capture_id 一次性、2 分钟过期，
  observe 刷新后失效。坐标用 PNG 原始尺寸，不是缩放后的显示尺寸。
- `evaluate` 是 observe + 常规交互无法完成时的最后手段；`--json` 时检查 `.ok`。
- 恢复协议：ref 失效 → observe 一次重试；fill 不一致 → 先观察字段；不可恢复 → 上报并 stop。

## 产物约定（axiom）

- screenshot/download 一律 `--out` 到运行目录 `evidence/` 下；
- console/network 留证时重定向到 `evidence/` 文件；
- credentials 结果写 `evidence/credentials-<domain>.json`。
