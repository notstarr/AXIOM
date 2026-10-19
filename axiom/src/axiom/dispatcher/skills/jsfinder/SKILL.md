---
name: jsfinder
description: 当需要测绘 Web 应用的 JS 能力树时使用：给一个或多个入口 URL，抓取 HTML/JS/chunk/Source Map，提取前端路由、API 接口、URL、域名 IP、用户相关、账号密码、云 Key、Auth、加解密等十类信息。只读抓取，不发破坏性请求；本 Skill 是脚本说明书，不决定测试方法论，也不把命中直接认定为漏洞。
---

# JSFinder 使用说明

给入口 URL，递归抓取页面引用的 JS 与 Source Map，用 109 条分类正则 + 校验器提取十类信息，产出一份 JSON 能力树底座。仅依赖 Python 标准库。

## 1. 定位与运行

```bash
JSFINDER_SKILL_DIR="$(axiom skill path jsfinder)"
JSFINDER="$JSFINDER_SKILL_DIR/scripts/jsfinder.py"
cd "$AXIOM_RUN_DIR" && "$AXIOM_PYTHON" "$JSFINDER" "<入口URL>"
```

- 必须先 `cd` 到当前运行目录：脚本读 `AXIOM_RUN_DIR` 把产物写在里面。
- 默认参数即可跑；需要登录态时加 `--cookie "<会话Cookie>"` 补认证后接口。
- 退出码：`0`=有结果；`2`=抓取成功但零命中（换入口或带登录态重跑）；`3`=入口全部不可达（在产物里如实记录收集失败，不得假装扫全）。
- 常用开关：`--max-js`（JS 文件上限，默认 1500）、`--max-seconds`（总时限，默认 180s，到点产出已有结果）、`--only "API接口,云Key"`（只跑部分类别）、`--threads`、`--timeout`。

## 2. 产物协议：一次运行一个文件，默认不覆盖

产物名 `jsfinder_result-<域名>-<路径>-<随机后缀>.json`；`--out` / `--tag` 可选，通常不用传。**默认写在运行目录根即可**：Bootstrap 与 Explore 收口时会自动把运行目录根下的全部 `jsfinder_result*.json` 登记进产物中心（`bootstrap.jsfinder` / `explore.jsfinder`），其它 worker 按 storage_key 或产物中心读取；不要手动拷进 `artifacts/evidence/`（那是漏洞证据目录），也不要用 `--out` 改到别处。因此：

- **每个入口各跑一次**（如 `/`、`/admin`、`/test`），各留一份产物。
- **登录态到手后必须带 Cookie 补跑一次**——认证后 chunk 里的接口树匿名爬不到；这次产出是另一份独立产物，不替换匿名轮那份，两份都保留。
- **凡在页面 / JS / Source Map 里发现的新前端路由或入口**（其它路径、管理台、其它子域），都用同一脚本补跑一次；不要以为一轮就覆盖了全站。
- 某次运行 JS 数撞到 `--max-js` 上限说明**没扫全**，应缩窄入口或提高上限再跑。
- 写资产清单、leads 或收口前，读**全部** `jsfinder_result*.json` 合并，不要只看最后一份。

## 3. 结果怎么用

JSON 顶层 `categories` 十类固定键（无命中为空数组）：

- `API接口`：按域 → 路径前缀分组，条目带 `METHOD path | query:.. | body:.. | header:..` 调用点注解，是接口测绘与越权对照的底座。
- `前端路由`：Vue/React/Angular 路由声明与导航调用，按站点分组，用来发现隐藏页面和管理台入口。
- `云Key` / `Auth` / `账号密码` / `加解密`：命中即高价值线索（泄露的 AK、JWT、私钥、硬编码口令），直接进 leads 并回原始请求确认「读即泄露」。
- `域名IP` / `URL` / `用户相关` / `其他`：补充资产面与参数来源。

命中只是线索：任何一类命中都要回到真实请求验证身份、权限和业务影响后，才能作为事实或成果上报。

## 4. 边界

- 只读抓取目标自身资源；不发 POST/PUT/DELETE、不 fuzz、不枚举口令（那些分别交给对应 Skill 与 Reason 的决定）。
- 不生成或改写字典、不扩写规则；`scripts/jsrules.json` 随 Skill 分发，确需替换时用 `--rules` 指向自备规则文件。
- 不替代 Reason 决定测什么：Explore 用它建能力树底座，Reason 只在 Intent 描述里点名要求使用它。
- 抓取失败或零命中如实记录，不得用推断补齐能力树。
