---
name: redteam-resources
description: 当红队任务里拿到或需要复用稳定立足点时使用：WebShell 连接登记与命令/文件操作（axiom webshell），C2 监听器/会话/任务下发（axiom c2）。连接与会话都绑定当前任务，仅红队模式 worker 可用；本 Skill 是 CLI 说明书与接力约定，不决定攻击方法论。
---

# 红队资源（WebShell / C2）使用说明

立足点（能执行命令的 webshell、反弹会话）是跨 Intent 复用的资产：**拿到就先登记，复用前先查询**，不要每个 Intent 重新打入口（噪声大、预算贵、还可能把脆弱入口打坏）。所有命令只在红队模式任务的 worker lease 内可用，漏洞模式调用会被拒绝。

## 1. WebShell：`axiom webshell`

| 命令 | 作用 |
|---|---|
| `axiom webshell list` | 本任务全部连接（id/名称/url/状态/最近探活） |
| `axiom webshell create --name N --url U [--family posix|windows] [--method POST] [--body-template 'cmd={{cmd}}'] [--query-template ''] [--headers-json '{}'] [--response-mode raw|between|regex --response-start S --response-end E] [--response-decode base64] [--remark R]` | 登记连接并绑定本任务 |
| `axiom webshell test <ws_id>` | canary（whoami/id/pwd）探活，回写 alive/dead |
| `axiom webshell exec <ws_id> '<命令>' [--timeout 30]` | 执行一条命令，返回输出与耗时 |
| `axiom webshell file <ws_id> --op list|read|write --path P [--content C]` | 文件三件套（posix 的 read 自动 base64 解码） |
| `axiom webshell logs <ws_id> [--limit 20]` | 执行审计流水（含 actor 与 Intent） |

模板要点：`--body-template` / `--query-template` 必须含 `{{cmd}}` 占位符；shell 用密码参数时把它写进模板（如 `pass=xxx&cmd={{cmd}}`）；响应有包裹标记时用 `between` + start/end，输出是 base64 时加 `--response-decode base64`。命令含特殊字符时整体用单引号包住。

## 2. C2：`axiom c2`

| 命令 | 作用 |
|---|---|
| `axiom c2 listeners` | 本任务监听器（id/name/kind/host:port/status） |
| `axiom c2 listener-create --name N --port P [--kind http|tcp] [--host 0.0.0.0]` | 建监听器（不自动启动） |
| `axiom c2 listener-start <cl_id>` / `listener-stop <cl_id>` | 启停 |
| `axiom c2 oneliner <cl_id>` | 回连 one-liner（http 给 bash/python 轮询版，tcp 给 mkfifo+nc 版）；把回连地址换成目标可达的地址后，通过已有命令执行能力投递 |
| `axiom c2 sessions [--listener-id clX]` | 上线会话（id/remote/info/status/last_seen/sleep） |
| `axiom c2 session-sleep <cs_id> --seconds N` | 调轮询间隔：交互需求高才调小，默认 5s 已够；长潜伏调大降低噪声 |
| `axiom c2 session-kill <cs_id>` | 标记会话死亡（tcp 会发 exit） |
| `axiom c2 exec <cs_id> '<命令>' [--wait 30]` | 下发任务：http 会话入队等 beacon 取走并回传；tcp 会话直读写至 1.5s 静默 |
| `axiom c2 tasks [--session-id csX]` / `axiom c2 task <ct_id>` | 任务队列与结果（status: queued/sent/completed/failed） |

http 监听协议：beacon `GET /index?sid=<自定id>` 取下一条命令（空=无任务），`POST /index?sid=<id>` 回传 base64 结果；新 sid 自动建会话。服务重启后监听器**不会**自动拉起，状态回落 stopped，需要重新 start。

## 3. 接力与收尾约定

- Explore 拿到命令执行能力后**当场登记**（webshell create / listener-start+oneliner 投递），并把连接或会话 id 写进 findings.md 与 leads.md（如「立足点：ws003 / cs002」），后续 Intent 的前置栏直接引用。
- 开工先 `webshell list` / `c2 sessions` 查现成立足点；status=dead 的先 `webshell test` 或看 last_seen 再决定复活还是重打。
- 上报成果时把立足点 id 写进 `--target-asset` 或描述，Reviewer 可按 logs/tasks 复核执行轨迹。
- 任务收尾：`listener-stop` 全部监听器、`session-kill` 残留会话；webshell 连接由人工在「红队资源」页归档或删除。
- 凭证与输出完整留档（不脱敏），但不要把会话凭证粘进 Intent 描述这类会进导出的字段。

## 4. 边界

- 只对本任务资源操作；跨任务复用由人工改绑或新建种子任务。
- 不在 webshell/C2 通道里跑破坏性命令（删除、加密、业务写入）；L5 类动作（持久化/横向/破坏）默认不做，除非任务授权明确。
- 监听器端口选高位非常用端口；host 用演练约定的回连网卡地址，不把管理面暴露公网。
