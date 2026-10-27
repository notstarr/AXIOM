---
name: vuln-report
description: 上报确认漏洞前阅读，包含去重、结构化复现和通用字段规范
---

# Vulnerability reporting

本 Skill 只给 **Explore** 阶段使用。发现不等于漏洞：必须有明确的实际行为和影响，并按同一根因去重。
Bootstrap 只记录线索；Reason 不得上报漏洞。

## 核心规则：复现是必填项

**没有可复现路径就不是已确认漏洞。** 写 `leads.md` 留痕即可，不要硬报。

上报时提交的复现不是叙述，而是**你真实打出去的请求与收到的响应**：

- `request` 必须是**标准 HTTP 请求报文**：请求行 + 关键 header + 空行 + body。
  **不能是 curl / shell 命令**——命令行会丢掉 header，而 header 往往正是漏洞关键。
  提交前会本地校验，交 curl 会直接报错。
- `response` 必须是**状态行 + 关键响应内容**。不要用 `...` 省略掉证明问题的片段（回显、token、报错）。
- `steps` 按执行顺序，1~12 步。每步要有 `title`（在做什么）和 `proves`（证明了什么）。

## 上报命令

不要创建漏洞 JSON（除下面这一份复现文件），也不要用旧的 `axiom-tool vulnerability report`。
在当前 Explore lease 中：

```bash
axiom explore vulnerability report \
  --title 'GET /api/users/{id} 未授权敏感信息泄露' \
  --type '未授权访问' \
  --severity high \
  --description '普通用户可读取其他用户的敏感资料' \
  --prerequisites '无需 Cookie 或 Authorization' \
  --impact '造成跨用户敏感数据泄露' \
  --reproduction-file repro.json \
  --affected-target '127.0.0.1:18083'
```

长文本可用对应的 `--*-file` 参数。
`--remediation`（修复建议）**不是必填**：交付报告里的这一节由审核/AI 富化补。
`--reproduction-file` 传 `-` 可读标准输入（报文有换行，用 heredoc 比拼参数可靠）。

## repro.json 示例

```json
{
  "entry_point": "POST /upload.php",
  "prerequisites": ["无需认证，任意匿名访问者即可上传", "上传目录可写且 PHP 可解析（实测成立）"],
  "steps": [
    {
      "title": "上传 PHP 文件到头像接口",
      "request": "POST /upload.php HTTP/1.1\nHost: target.test\nContent-Type: multipart/form-data; boundary=----X\n\n------X\nContent-Disposition: form-data; name=\"avatar\"; filename=\"shell.php\"\nContent-Type: application/x-php\n\n<?php echo \"PWNED\"; ?>\n------X--",
      "response": "HTTP/1.1 200 OK\nContent-Type: application/json\n\n{\"code\":1000,\"url\":\"/uploads/shell.php\"}",
      "proves": "上传接口不校验扩展名与 MIME，任意 .php 落地并可直接访问"
    },
    {
      "title": "访问落地的 webshell",
      "request": "GET /uploads/shell.php?c=id HTTP/1.1\nHost: target.test\n\n",
      "response": "HTTP/1.1 200 OK\n\nuid=33(www-data) gid=33(www-data)",
      "proves": "落地的 PHP 被解析执行，取得命令执行"
    }
  ]
}
```

`chain_input` / `chain_gain` 两个字段留给红队模式；漏洞模式可以不填。

## 常见错误

| 错误 | 会被拒绝，因为 |
|---|---|
| `request` 写成 `curl -X POST ...` | 命令行丢了 header，读者无法照抄复现 |
| `response` 只写 `{"code":1000}` | 缺状态行，无法判断是哪次请求 |
| 步骤里没有 `proves` | 读者不知道这一步证明了什么 |
| 一条请求塞十几个断言 | 拆成多步，每步一个结论 |
| 单条报文超过 16KB | 只保留能证明问题的关键 header 与 body 片段 |

服务端会自动补齐 `intent_id`、`source=explore`、`reported_by` 和 `status=待验证`，
并按当前 lease 校验、去重和持久化。

## 不要再写证据文件

`artifacts/evidence/` 与 `artifacts/work/` 已停用：收口不登记，运行目录随沙箱清理。
证明就在报文里。需要复查的结论写进 `findings.md` / `leads.md`。

不要为了补证据重新执行大型测试，也不要编辑 `explore_result.json`；
阶段完成时由代码生成 Fact、正式 Artifact 和可用的证据关系。
Task 开启外部上报时，Server 会经 Adapter 转换内部对象。
