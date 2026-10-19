---
name: redteam-scoring
description: 红队成果上报前必读。护网累计计分表、申报规则与上报命令
---

# 护网行动 · 红队成果评分表

Explore 上报前按本表自评 `--points`。Reviewer 按同一张表核定。拿不准选低档。虚报会被降档，没有可复现证据会被驳回。

累计计分，任务总分不设上限。一条成果的分 = 单价 × 本次新取得的数量。`--type` 只用 `permission`、`data`、`credential`、`pivot`。

## 一、资产控制

外网、内网、DMZ 同价。`--type permission`。

| 成果 | 单位 | 分 |
|---|---|---:|
| 核心服务器 · 最高权限（root / SYSTEM / 管理员） | 每台 | 20 |
| 核心服务器 · 普通权限（Webshell / 低权账号 / 服务账号） | 每台 | 10 |
| 一般服务器 · 最高权限 | 每台 | 10 |
| 一般服务器 · 普通权限 | 每台 | 5 |
| 边缘资产 · 最高权限（旁站 / 测试环境 / 无数据服务） | 每台 | 4 |
| 边缘资产 · 普通权限 | 每台 | 2 |
| 办公终端 / 个人 PC · 最高权限 | 每台 | 3 |
| 办公终端 / 个人 PC · 普通权限 | 每台 | 1 |
| Web 应用管理员 · 核心应用（可操作业务或数据） | 每个 | 10 |
| Web 应用管理员 · 一般应用 | 每个 | 5 |
| Web 应用管理员 · 边缘应用 | 每个 | 2 |
| 核心网络设备 · 控制（核心交换机 / 出口防火墙 / VPN 网关） | 每台 | 15 |
| 一般网络设备 · 控制（接入层 / 一般路由） | 每台 | 5 |

有效办公账号（邮箱 / OA / VPN / 业务系统，可实际登录）用 `--type credential`，每个 1 分，合计上限 10 分。

## 二、数据获取

`--type data`。按可访问库表的记录总数达档计分，不按条数线性累加。`--sample-count` 写记录数，并附记录数证明。样本原文完整存入 evidence（复核与后续任务要复用，不脱敏、不截断字段）。

| 成果 | 规模 | 分 |
|---|---|---:|
| 核心业务数据（用户 / 交易 / 资金 / 订单） | 1 千 ~ 1 万 | 2 |
| 核心业务数据 | 1 万 ~ 10 万 | 4 |
| 核心业务数据 | 10 万 ~ 100 万 | 10 |
| 核心业务数据 | 100 万以上 | 15 |
| 重要数据（客户 / 财务 / 人力 / 合同） | 1 千 ~ 1 万 | 1 |
| 重要数据 | 1 万 ~ 10 万 | 2 |
| 重要数据 | 10 万 ~ 100 万 | 6 |
| 重要数据 | 100 万以上 | 10 |

不足 1000 条、其它非敏感数据：不计分，不要上报。

## 三、核心目标

战略级单项，不另入第一类。`--type permission`。

| 成果 | 单位 | 分 |
|---|---|---:|
| 靶标系统 · 完全控制 | 每个 | 60 |
| 靶标系统 · 部分控制（关键功能或核心数据可达） | 每个 | 30 |
| 域控（DC）· 完全控制 | 每个 | 50 |
| 域管理员权限（未控制 DC 本体） | — | 30 |
| 统一认证 / 堡垒机 / 云管平台 / 虚拟化平台 · 控制（含 vCenter、K8s 集群） | 每个 | 25 |

## 四、纵深穿透

`--type pivot`。

| 成果 | 单位 | 分 |
|---|---|---:|
| 穿透一层网络隔离（DMZ→内网、办公网→生产网、生产网→工控网等） | 每层 | 10 |
| 跨域 / 跨租户控制（多域环境控制第二、第三个域） | 每个 | 15 |

## 资产等级

- 核心：核心业务系统、核心数据库、统一认证、堡垒机、云管平台、VPN 管控
- 一般：普通生产服务器、内部业务系统、运维跳板、办公应用
- 边缘：旁站、测试 / 预发、无数据服务、周边设备

任务或评测方已归级的，用归级。没有归级时按上面判断，并在标题或描述里写明等级。拿不准降一档。

## 计分规则

1. 同一目标只计一次。已有低档 verified 时，新的高档只申报差额，使该目标合计等于最高档。已有同档或更高档，不要再报。
2. 核心目标不另按服务器、Web 管理员或网络设备加分。
3. 凭证已经换成主机、域控或应用管理员控制的，按控制成果计，不另计办公账号分。只有凭证、不能登录的不计分。
4. 仅扫描探测、未取得权限的不计分，不要上报。
5. 每条计分成果都要有可复现证据：请求响应、命令回显、录屏或导出样本。

## 上报示例

核心应用管理员，10 分：

```bash
axiom explore result report \
  --points 10 \
  --type permission \
  --title '核心应用管理员：成员管理可建角色并赋权' \
  --description '普通成员会话创建角色并批量赋权，变更已落库。资产等级：核心应用。' \
  --principal 'user:member' \
  --capability-gained 'app:admin' \
  --target-asset 'member-management' \
  --evidence-path 'evidence/request.txt' \
  --evidence-path 'evidence/response.txt'
```

核心服务器最高权限，1 台 20 分：

```bash
axiom explore result report \
  --points 20 \
  --type permission \
  --title '核心服务器最高权限：app-01 root' \
  --description '命令回显为 uid=0。资产等级：核心。' \
  --principal 'user:member' \
  --capability-gained 'host:root' \
  --target-asset 'app-01' \
  --evidence-path 'evidence/id.txt'
```

核心业务数据 10 万条，10 分：

```bash
axiom explore result report \
  --points 10 \
  --type data \
  --title '核心业务数据：订单表 12 万条可读取' \
  --description '订单表可访问记录数 120000，已附计数证明。样本原文在 evidence/。' \
  --principal 'user:member' \
  --data-target-id 'orders' \
  --sample-count 120000 \
  --data-field 'order_id' --data-field 'status' \
  --evidence-path 'evidence/count.txt' \
  --evidence-path 'evidence/sample.json'
```
