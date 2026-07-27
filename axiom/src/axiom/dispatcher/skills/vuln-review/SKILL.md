---
name: vuln-review
description: 人工审查已有漏洞声明时参考，核查关联 Findings 与证据 Artifact
disable-model-invocation: true
---

# 漏洞审查

本 Skill 仅描述人工审查规则。Reason 不上报、修改或审查漏洞；Explore 新上报的漏洞固定进入“待验证”。

## 审查步骤

1. 从任务漏洞接口读取记录。
2. 打开 `finding_artifact`，确认漏洞叙述与本轮确认事实一致。
3. 逐个打开 `evidence_artifacts`，核查请求、响应、工具输出和证明是否直接支撑结论。
4. 检查 `severity`、`affected_target`、`impact` 和 `remediation` 是否与实际证据一致。
5. 通过漏洞管理界面或 PATCH 接口修改结构化字段与状态。

## 状态规则

- 证据充分：改为“已验证”。
- 同根因重复：改为“重复”。
- 决定忽略：改为“忽略”，并填写不少于4个字的原因。
- 仍需人工核查：保持“待验证”。

不得把复现步骤、curl 或长响应重新复制进漏洞数据库；需要补充的原始材料应先保存为 Artifact，再关联到漏洞。
