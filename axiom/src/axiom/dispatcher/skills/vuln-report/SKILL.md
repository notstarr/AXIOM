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
- 每步的报文放在 `exchanges` 里：一组 `{request, response}` 就是一次真实收发，按顺序 1~8 组。
  **一步可以有多个报文**：同一个结论要两个请求才成立时（先申领上传凭证、再拿凭证覆盖文件），
  各开一组。不要把两个请求拼进同一个 `request`——拼起来的报文读者分不清哪行响应对应哪个请求，
  也没法重放。换了结论就另开一步。

## 上报命令

不要创建漏洞 JSON（除下面这一份复现文件），也不要用旧的 `axiom-tool vulnerability report`。
在当前 Explore lease 中：

```bash
axiom explore vulnerability report \
  --title 'GET /api/users/{id} 未授权敏感信息泄露' \
  --type '未授权访问' \
  --severity high \
  --description '普通用户可读取其他用户的敏感资料' \
  --impact '造成跨用户敏感数据泄露' \
  --reproduction-file repro.json \
  --affected-target '127.0.0.1:18083'
# 前提已写在 repro.json 里，所以这里不传 --prerequisites
```

**利用前提写进 `repro.json` 的 `prerequisites`**（数组，一条一个条件）——报告的「利用前提」一节
展示的就是这一份，重放与审核也读它。`--prerequisites` 只是它的一行概括，仅在复现没有条件时才被
渲染；复现里已经有条件时可以不传，别把同一批条件抄两遍。

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
      "title": "申领上传凭证并用他人客户编码上传覆盖文件",
      "exchanges": [
        {
          "request": "POST /admin/v1/file/temporary-credential HTTP/1.1\nHost: erp.example.com\nauthorization: <攻击者 JWT>\nContent-Type: application/json\n\n{\"storage\":\"file_service\",\"dir\":\"erp/import/customer/\",\"file_name\":\"poc.xlsx\"}",
          "response": "HTTP/1.1 200 OK\n\n{\"code\":1000,\"data\":{\"path\":\"000/erp/import/customer/poc.xlsx\"}}"
        },
        {
          "request": "POST https://oss.example.com/ HTTP/1.1\nContent-Type: multipart/form-data; boundary=----X\n\n(key=000/erp/import/customer/poc.xlsx, 文件内容中客户编码为他人客户)",
          "response": "HTTP/1.1 200 OK\n\n{\"code\":1000,\"message\":\"请求成功\"}"
        }
      ],
      "proves": "攻击者仅用出库凭证即可向他人客户编码的导入目录覆盖文件"
    },
    {
      "title": "访问落地的 webshell",
      "exchanges": [
        {
          "request": "GET /uploads/shell.php?c=id HTTP/1.1\nHost: target.test\n\n",
          "response": "HTTP/1.1 200 OK\n\nuid=33(www-data) gid=33(www-data)"
        }
      ],
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
| 两个请求拼进同一个 `request` | 读者分不清哪行响应对应哪个请求，且无法重放；应各开一组 `exchanges` |
| 单条报文超过 16KB | 只保留能证明问题的关键 header 与 body 片段 |

服务端会自动补齐 `intent_id`、`source=explore`、`reported_by` 和 `status=待验证`，
并按当前 lease 校验、去重和持久化。

## 不要再写证据文件

`artifacts/evidence/` 与 `artifacts/work/` 已停用：收口不登记，运行目录随沙箱清理。
证明就在报文里。需要复查的结论写进 `findings.md` / `leads.md`。

不要为了补证据重新执行大型测试，也不要编辑 `explore_result.json`；
阶段完成时由代码生成 Fact、正式 Artifact 和可用的证据关系。
Task 开启外部上报时，Server 会经 Adapter 转换内部对象。
