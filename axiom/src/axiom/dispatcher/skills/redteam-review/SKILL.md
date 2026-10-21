---
name: redteam-review
description: 红队成果审核前必读。按护网评分表核定分
---

# 红队成果审核

审核对象是红队成果。先读 `redteam-scoring`，按那张表核定 `points_awarded`。不以自评分为准。

## 审查

1. 证据要能复现声明的控制、数据或穿透。只有扫描、接口存在、未取得权限 → 驳回。
2. 对照评分表核定。单价 × 本次新取得的数量。拿不准降档。
3. 同一目标只计一次。已有低档 verified 时，核定分 = 高档 − 已核定低档。已有同档或更高档 → `duplicate`，0 分。
4. 核心目标（靶标、域控、域管、统一认证、堡垒机、云管、虚拟化）不另入资产控制类。
5. 有效办公账号每个 1 分，已核定合计达到 10 分后，超出部分核 0。
6. 数据要有记录数证明。不足 1000 条或非敏感数据核 0 并驳回。样本不得出现明文敏感值。
7. 越出任务 Scope 的成果驳回。
8. **对象必须是公网入口。** `target_asset` 只写内网 IP（如 `192.168.0.95`、`10.192.192.205`）时，先确认成果实际是从哪个公网入口打进去的（如 `shk.kkguan.com/universe`、`git.kkgroup.cn`），把对象改成公网入口。落点在内网主机是正常的——只要入口是公网、且路径是「公网漏洞 → 立足点 → 内网」，就按正常成果核定，不要因为出现内网 IP 就当作违规打内网而驳回。
9. **只有这几种情形才算违规打内网**：①经 bsk 浏览器直接访问私网地址；②没有任何已确认漏洞或既存立足点，就把内网地址当目标直接打。两种情况都驳回。经目标自身 SSRF、已控主机/CI runner、或公网应用漏洞落到内网的，不算违规。

## 裁决

- `verified`：必须给 `--points`。落库以核定分为准。
- `rejected`、`duplicate`、`needs-human`：0 分。理由写明缺什么，或与哪条成果、哪个目标重复。

## 示例

核心应用管理员，核定 10 分：

```bash
axiom review decision submit verified \
  --reason '普通成员会话创建角色并批量赋权，变更已落库。核心应用管理员，10 分。' \
  --points 10
```

核心服务器最高权限，核定 20 分：

```bash
axiom review decision submit verified \
  --reason '命令回显 uid=0，目标是核心服务器 app-01。最高权限 20 分。未另计 Web 管理员。' \
  --points 20
```

同一目标已有低档，只核差额：

```bash
axiom review decision submit verified \
  --reason 'app-01 已核定普通权限 10 分。本次证明 root，最高权限 20 分，差额 10 分。' \
  --points 10
```

证据不足：

```bash
axiom review decision submit rejected \
  --reason '只有接口存在性，没有成功取得权限或数据的可复现证据。'
```

重复：

```bash
axiom review decision submit duplicate \
  --reason '与 x003 为同一目标 app-01 的同档控制，按最高档只计一次。'
```
