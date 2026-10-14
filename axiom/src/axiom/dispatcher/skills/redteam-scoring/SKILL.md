---
name: redteam-scoring
description: 红队成果上报前必读，包含积分评分表、自评规则与上报命令
---

# RedTeam scoring（红队积分评分表）

本 Skill 只给 **Explore** 阶段使用。每条红队成果按本表**自评申报分**（`--points`，1-50），审核员会按同一张表核定——虚报会被降档，证据不足会被驳回。拿不准档位时选低档。

## 评分表

| 类别 | 达成项 | 分值 | 认分条件 |
|---|---|---:|---|
| permission | 获取初始身份之外的有效会话/身份 | 10 | 登录态可复用，有请求/响应证据 |
| permission | 水平越权读取其他身份数据 | 15 | 两个身份的对照证据 |
| permission | 垂直提权（可调用管理功能） | 30 | 管理接口成功调用证据 |
| permission | 完全管理员接管（可建号/改权限） | 40 | 创建/修改成功证据 |
| permission | 服务器命令执行（RCE） | 50 | 命令回显证据，执行范围受限 |
| data | 敏感字段级读取（脱敏样本） | 10 | 字段在允许清单内、样本已脱敏 |
| data | 批量读取能力（数量/字节达标） | 20 | 样本数 + 字节数 + 字段清单齐全 |
| data | 核心身份数据（证件/手机号等） | 25 | 字段类型证据（脱敏展示） |
| credential | 低价值凭证（普通会话 token） | 10 | 只交引用 ID/哈希 |
| credential | 高价值凭证（API key / 内部系统凭证） | 25 | 引用 ID/哈希 + 用途说明 |
| credential | 基础设施凭证（DB / 云 AK/SK） | 30 | 引用 ID/哈希 + 可用性证明 |
| pivot | 打通一个新内网边界/系统 | 20 | 新位置可达证据 |
| pivot | 进入核心区（管理后台/核心库可达） | 30 | 位置与访问证据 |

## 计分规则

1. **同一能力只计一次**：同任务同链已 verified 过的 capability，重复上报标 duplicate 计 0 分
2. **单条上限 50 分**：一条成果覆盖多档时，按最高档申报
3. **证据决定档位**：申报分必须有对应证据支撑；证据只够低档就报低档
4. 数据类成果：样本数/字节数必填，样本必须脱敏，禁止明文敏感值

## 上报命令

```bash
axiom explore result report \
  --points 30 \
  --type permission \
  --title '垂直提权：普通成员可调用成员管理接口' \
  --description '复用普通成员会话成功调用 POST /api/members/promote，将自身提升为管理员' \
  --principal 'user:member' \
  --capability-gained 'tenant:admin' \
  --capability-before 'documents:read' \
  --target-asset 'member-management' \
  --evidence-path 'evidence/request.txt' \
  --evidence-path 'evidence/response.txt'
```

数据类示例（必须带样本规模与字段）：

```bash
axiom explore result report \
  --points 20 \
  --type data \
  --title '越权批量读取客户记录' \
  --description '水平越权枚举 customer 接口，读取 5 条记录（已脱敏）' \
  --principal 'user:member' \
  --data-target-id 'customer-records' \
  --sample-count 5 --sample-bytes 2048 \
  --data-field 'id' --data-field 'status' \
  --evidence-path 'evidence/sample.json'
```
