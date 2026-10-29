---
name: browser-skill
description: |
  使用 bsk 命令驱动专用 Chrome profile（VPS 远程模式经 bsk daemon），
  唯一用途是获取 scope 内目标的登录态（Cookie/localStorage）；
  不用于内网访问、内网探测或利用。要求 Mac 扩展已 Remote 配对。
  本版本为 axiom 定制：凭据提取是渗透测试的合法产出；scope 外目标禁止访问。
  命令面与上游 bsk 0.3.3 对齐（网站调试用 debug；network 仅索引；
  extract 结构化取数；credentials 是 fork 专有，上游从未提供）。
---

# browser-skill（axiom 定制）

通过 `$AXIOM_BSK` 包装器驱动专用 Chrome profile。自动化在独立的 **Agent
Window** 中进行。VPS 远程模式只使用 task-created tabs，不借用用户标签；
upload/download 不可用；`credentials` 只读取专用 profile。

**不要**将该 skill 用于：无浏览器场景、扩展安装、或用户只想要操作说明的情况。

## 用途边界：这个浏览器只用来取登录态

配对的 Chrome 跑在**操作者办公网里的一台 Mac** 上，出口就是那台机器的网卡。它发出的每个请求都是以操作者身份、从内网内部发出的，所以：

- **唯一合法用途**：打开 scope 内、确实需要登录态的目标，完成登录（或复用已有登录态），用 `credentials` 取出该域 Cookie/localStorage 交给 VPS 侧继续工作，然后 `session stop`。
- **它不是跳板**：不得借它访问内网地址或做内网探测、扫描、利用、横向。内网作业只能走目标自身 SSRF、已控主机或 C2 会话。
- **换写法不算绕开**：`tab create --url`、`evaluate` 里 `fetch`、`debug replay` 指向内网，与直接 `navigate` 是同一件事。
- 拿不到内网执行点时按无进展上报（`request-help` 或写 leads 交回 Reason），不要退化成用浏览器硬连内网。

**凭证卫生**：不得从页面提取/外传与测试无关的凭据；Cookie/凭据数据只写
`evidence/` 目录（证据完整性优先），不要把凭据明文写进 findings/leads 正文。

## 必需生命周期

```text
1. "$AXIOM_BSK" session start [--no-focus]                # 记下输出的 4 字符 session id
2. "$AXIOM_BSK" <命令> --session <id>                     # 所有会话级命令都要带
3. "$AXIOM_BSK" session stop <id>                         # 成功与失败路径都必须执行
```

不要依赖空闲超时清理。目标达成即 stop；stop 关闭本次 session 的标签。
远程 daemon 常驻 VPS，不要手工启动/停止；排障用 `bsk doctor` / `bsk status`。
Agent Window 不需要打断用户时，session start 加 `--no-focus`。
传输/连接问题重试一次仍存在时运行 `bsk doctor`。

## 浏览器身份（VPS 远程模式）

远程模式只连接配对的专用 Chrome profile（`bsk browsers` 应显示一个在线设备）。
不要使用 `--browser` 在多个实例间切换；需要多身份时，配对多个专用 profile/设备。
session 创建的所有标签都在 Agent Window；只使用 `tab list --scope agent` 查看本 session 的标签。

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
不要自己编 tab id，也不要跨无关任务一直占着标签。
**`tab create --url` 与 `navigate` 的地址同样受上文「用途边界」约束**——换到后台
标签开一个内网地址，和直接 navigate 是同一次违规。

`tab list` 在远程模式只允许 `--scope agent`（本 session 的 Agent Window 标签）；
用户标签不可见、不可借用，wrapper 会拒绝 `--scope user` / `--scope all` / 缺省 scope。

## 凭据提取（axiom 定制）

```bash
"$AXIOM_BSK" credentials --domain <domain> --session <id>
```

远程模式只读取专用 profile；不要试图借用用户标签扩大范围。
**`--domain` 必须是本次任务 scope 内的域**；对无关域批量拉 Cookie 属于越界，
即便技术上能取到也会被判违规。取到的凭据用于「把会话交给 VPS 侧继续验证」，
不应成为在本机发起内网请求的理由。
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
observe   snapshot   get-html   screenshot   console   network   debug   extract
click   hover   wheel   scroll-to   focus   blur   fill   select   press   evaluate
tab list|create|close|select   window resize   emulate
wait-for-element   video start|stop
request-help   record start|stop
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

- **`get-html` 只认 `--ref`，没有位置参数**：`get-html @e1` 会报
  `unexpected argument`，必须写 `get-html --ref @e1`；不带 `--ref` 返回整页 HTML。
- **`focus` / `blur` 必须带目标**（`<ref>`、`--ref`、`--selector` 三者其一），
  不存在无参的 `blur`。要清焦点就传刚聚焦的那个元素。
- **`navigate-back` / `navigate-forward` 遇到 bfcache 恢复会假超时**。页面确实回退
  成功了（`final_url` 正确、DOM 已就绪），但 bfcache 恢复**只触发 `commit`**，
  `load` 与 `domcontentloaded` 都不会再发一次，于是命令等满 15s 返回
  `reached=timeout`、退出码 4。实测同一操作：

  | `--wait-until` | 耗时 | 结果 |
  |---|---|---|
  | `load`（默认） | 15.00s | `reached=timeout` |
  | `domcontentloaded` | 15.01s | `reached=timeout` |
  | `commit` | **0.01s** | `reached=commit` ✅ |

  **加长 `--timeout` 无效**（40s 仍超时），因为那个事件永远不会来。历史导航一律用
  `commit`：

  ```bash
  "$AXIOM_BSK" navigate-back --session <id> --wait-until commit
  "$AXIOM_BSK" navigate-forward --session <id> --wait-until commit
  ```

  若看到 `reached=timeout` 但 `final_url` 已变，**先回读 `location` 再判定**，
  不要据此认定导航失败或重试。`navigate`/`reload` 是真实加载，默认 `load` 正常。
- `record start` 写的是 **trace v3 bundle 目录**，不能给 `.json` 文件路径：
  `record start --output <目录>`（产出 `<目录>/trace.json` 与 `<目录>/states/`）。
  它自己开 Agent Window 并阻塞，靠 `record stop` 从另一个终端结束。
- `video status|stop|list` **不接受 `--session`**，用 `--recording <id>` /
  `--browser <id>` 定位；只有 `video start` 要 `--session`。
- 远程模式下 `upload` / `download` 恒返回 `not supported by the current build`，
  这是协议层限制，不是配置问题；需要文件落地就在 VPS 侧用 curl/python 取。

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
  **`rules`/`replay` 只能对 scope 内的公网目标重放**；把抓到的请求重放成内网地址
  （或改 Host/URL 指向私网）与直接 navigate 同等违规。
- **`extract` 取表格/列表正文**（0.3.3 新增；比 `get-html` + 正则省一轮，且保留列序、
  分页与跨 frame 来源）。后台列表页取数优先用它：

  ```bash
  "$AXIOM_BSK" extract discover --session <id>                          # 列出可抽取容器
  "$AXIOM_BSK" extract table --session <id> --ref @e12 --format csv \
      --out "<运行目录>/evidence/orders.csv"                             # 直接落 CSV
  "$AXIOM_BSK" extract list --session <id> --selector "ul.rows" \
      --item-selector "li" --fields-json '{"fields":["name","amount"]}'
  ```

  `--max-rows/--max-columns/--max-bytes` 是硬上限，`--timeout-ms` 是渲染层采集预算
  （默认 5000）。返回里带完整性标记：**被上限截断时必须如实上报截了多少行**，
  不要把部分结果说成全量。
- `wait-for-element`（0.3.3 新增）替代 `wait-ms` 硬等：`--state visible|hidden|attached|detached`
  带 deadline，元素永远不出现时按失败返回，而不是静默超时。
- `video start/stop`（0.3.3 新增，**默认关**）录任务的 H.264 MP4，10 分钟 / 256 MiB /
  浏览器侧保留 24h。仅在有明确复现需求时开，不要当默认取证手段（体积大且含页面全部内容）。
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
  **不要用它在页面里 `fetch`/`XMLHttpRequest`/`WebSocket` 打内网地址**——那是
  绕开「用途边界」的同一件事。
- 恢复协议：ref 失效 → observe 一次重试；fill 不一致 → 先观察字段；不可恢复 → 上报并 stop。

## 产物约定（axiom）

- **Explore 阶段不截图**：`artifacts/evidence/` 已停用，证明内容是随上报提交的结构化复现报文。
  截图由审核者事后补充（`POST /api/v1/tasks/{task_id}/vulnerabilities/{vuln_id}/evidence`）。
- console/network 的原始输出重定向到运行目录临时文件即可，不要再往 `evidence/` 里放；
- credentials 结果直接写进 `findings.md` / lead 结论（cookie/token 全文），不落 evidence 文件。
