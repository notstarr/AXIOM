---
name: browser-skill
description: |
  使用 bsk 命令驱动用户的真实浏览器（已登录态）：访问页面、填表、点击、
  接口探测、抓取数据。要求本机 bsk CLI 与浏览器扩展已连接。
  本版本为 axiom 定制：凭据提取是渗透测试的合法产出；scope 外目标禁止访问。
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

## 凭据提取（axiom 定制）

```bash
"$AXIOM_BSK" credentials --domain <domain> --session <id>
```

返回该域全部 Cookie（**含 HttpOnly**）+ 已打开页面的 localStorage/sessionStorage，
以及拼好的 `Cookie` 请求头。渗透测试中会话凭据是合法产出：用于认证后接口探测
（Authorization/Cookie 头）与对象级越权对照。**结果只写 evidence/ 目录**，
findings/leads 正文引用 evidence 路径，不要内联明文。

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
observe   snapshot   get-html   screenshot   console   network
click   hover   wheel   scroll-to   focus   blur   fill   select   press   evaluate
tab list|create|close|select|borrow|return   window resize   emulate
upload   download   request-help   record start|stop
credentials --domain <domain> --session <id>   # axiom 定制
```

常用易错参数：

```text
"$AXIOM_BSK" fill <ref> --value <text>
"$AXIOM_BSK" select <ref> --value <option-value>
"$AXIOM_BSK" screenshot --full-page --out <path>          # 整页拼接(懒加载页用 --scope current)
"$AXIOM_BSK" scroll-to <ref> --session <id>               # 滚动到指定元素(触发懒加载)
"$AXIOM_BSK" wheel --session <id>                         # 原生滚轮事件
"$AXIOM_BSK" click <ref> --capture <capture-id> --image-x <x> --image-y <y>
"$AXIOM_BSK" upload <ref> --file <path>
"$AXIOM_BSK" download <ref> --out <path>

- `network` 是 DevTools 面板的 worker 版：默认索引只列 API 流量（静态资源与
  chrome-extension:// 噪音已滤除），响应体/请求头不出现在索引里。
- 索引 + 详情两段式：`network --clear` 记游标 → 触发功能（click/fill）→
  `network` 看增量索引（每包一行）→ `network --detail <seq>` 看单包全量
  （请求头/请求体/响应头/响应体）。禁止无 clear 连续刷 network；响应体全文
  只经 `--detail` 按需取，截断时用 `--since/--limit/--url` 缩窗。
- `network --url <子串>`（大小写不敏感）与 `--all`（含静态资源）按需过滤；
  序号被环形挤出时 detail 返回空——索引行仍在，说明该包发生过。
- `emulate` 对单个 tab 应用视口/UA/触屏覆盖；新 tab 不继承；`--off` 恢复。
- `scroll-to`/`wheel` 用于无限滚动与懒加载页：滚一段 → observe/network 看增量，
  不要用 `evaluate window.scrollTo`（会绕过页面自己的滚动监听）。
- `screenshot --full-page` 拼接整页证据（默认 --scope follow 跟随追加内容；
  loading_stalled 30s 会报错，不要盲目加 --timeout）；`--scope current` 只截已加载区间。
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
