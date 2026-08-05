---
name: vuln-review
description: Reviewer 使用 CVSS v4.0 风格维度进行主动复现、影响校准、重复判定和四状态裁决的方法
---

# 漏洞审核方法

本 Skill 供 Reviewer Worker 使用。Finding、Evidence、漏洞正文和原严重等级都只是**待核验声明**，不是默认可信结论。审核者必须在 Scope、Guard、凭证和安全边界内尽可能独立复现，再决定：

```text
verified / duplicate / ignored / needs-human
```

Reviewer 不重做完整 Explore，不扩大攻击面；它只围绕当前漏洞的核心声明做最小、可审计的真假验证和影响校准。

## 必读附件

四种裁决各有一份完整 `artifacts/review.md` 示例：

| 候选裁决 | 必须查看的附件 |
|---|---|
| `verified` | `references/verified-example.md` |
| `duplicate` | `references/duplicate-example.md` |
| `ignored` | `references/ignored-example.md` |
| `needs-human` | `references/needs-human-example.md` |

执行规则：

1. 读取本 `SKILL.md` 后，先完成初步验证设计，不要立刻裁决。
2. 得出候选状态后，必须使用 `read` 打开对应附件并对照其结构、证据粒度和裁决门槛。
3. 若在两个状态之间犹豫，两个附件都要读取。例如无法确认应 `ignored` 还是 `needs-human` 时，同时读取这两份示例。
4. 附件是质量示例，不是可复制结论。必须替换为当前 ReviewRun 的真实对象、请求、对照、证据和不确定性。
5. 提交 `axiom review decision submit` 前，确认自己的 `artifacts/review.md` 至少达到对应附件的可审计程度。

附件路径以 `axiom skill path vuln-review` 返回目录为根目录。例如：

```text
<vuln-review目录>/references/verified-example.md
```

## 一、先把漏洞声明拆成可验证命题

从 `review_target.json`、Finding 和 Evidence 中提取：

- **对象**：主机、端点、页面、资源类型、租户、角色、客户端上下文；
- **安全边界**：未登录与已登录、用户 A 与用户 B、普通用户与管理员、租户 A 与租户 B、主站与微应用；
- **输入与前置条件**：参数、Header、Cookie、Token、资源 ID、业务状态、用户交互；
- **声称现象**：读到了什么、改写了什么、执行了什么、绕过了什么；
- **声称影响**：机密性、完整性、可用性以及是否影响后续系统；
- **可证伪条件**：出现什么结果时应判定声明不成立或影响被夸大。

若无法把漏洞描述改写为可验证命题，不能直接判 `verified`；应先补读材料或最终选择 `needs-human`。

## 二、按 CVSS v4.0 风格维度审核

这里借用 CVSS v4.0 Base Metrics 的维度来防止遗漏关键条件，**不是要求 Reviewer 充当自动评分器**。状态裁决与严重等级是两件事：漏洞可被验证但影响很低；请求可达也可能完全没有安全影响。

### 可利用条件

| 维度 | 审核问题 | 需要的证据 |
|---|---|---|
| Attack Vector（AV） | 攻击从网络、相邻网络、本地还是物理路径发生？ | 实际入口、访问路径和 Scope |
| Attack Complexity（AC） | 除攻击者控制外，是否需要不可预测条件或竞态？ | 稳定复现次数、失败条件、竞态说明 |
| Attack Requirements（AT） | 是否依赖特定部署、配置或前置运行状态？ | 环境条件、业务状态、配置证据 |
| Privileges Required（PR） | 攻击前需要什么身份和权限？ | 无身份/低权限/高权限对照 |
| User Interaction（UI） | 是否需要受害者点击、访问或执行动作？ | 真实交互链或无交互证明 |

### 受影响系统

| 维度 | 审核问题 | 需要的证据 |
|---|---|---|
| Vulnerable System Confidentiality（VC） | 当前系统是否泄露了本不应读取的数据？ | 数据内容、归属、敏感性和越权边界 |
| Vulnerable System Integrity（VI） | 当前系统是否允许未授权修改或执行？ | 修改前后状态、服务端最终状态 |
| Vulnerable System Availability（VA） | 当前系统是否出现可归因的可用性影响？ | 最小安全验证或直接证据；禁止 DoS 实测 |

### 后续系统影响

| 维度 | 审核问题 | 需要的证据 |
|---|---|---|
| Subsequent System Confidentiality（SC） | 是否进一步泄露其他系统或安全域的数据？ | 明确的跨系统数据流和访问结果 |
| Subsequent System Integrity（SI） | 是否进一步控制或修改其他系统？ | 可验证的信任链、执行链或状态变化 |
| Subsequent System Availability（SA） | 是否会影响其他系统可用性？ | 直接架构证据；禁止扩大破坏性验证 |

### 使用要求

- 每个维度写成“结论 + 当前证据”，不要只填缩写。
- 不知道就写“未知”，并说明缺失哪种身份、环境、数据或操作条件。
- 只有所有 Base Metrics 都有证据时，才可附一条参考 Vector；不要凭猜测补齐指标，也不要虚构分数。
- 原漏洞严重等级明显夸大或缩小时，在 `review.md` 记录偏差，但 Reviewer 不改写原等级字段。
- CVSS 维度不能替代 `whatisvul` 的漏洞成立标准，也不能替代真实复现。

## 三、主动验证与对照

目标可达且操作安全时，至少完成：

```text
一次新的独立验证 + 一个只改变关键变量的对照
```

常见对照设计：

- 身份/越权：无身份、用户 A、用户 B、本人资源、他人资源；
- 输入处理：无害测试值、正常值、无效值，保持其他条件一致；
- 信息暴露：未登录与登录响应，检查实际内容而不只看 HTTP 状态码；
- 业务逻辑：满足与不满足前置状态，比较服务端最终状态；
- 客户端问题：真实渲染与安全对照页面，记录 DOM、网络、控制台或弹窗；
- 配置问题：当前配置与预期安全配置的可观察差异。

验证记录必须包含：

- 时间、目标、使用的身份/角色；
- 请求或操作的最小可复现描述；
- 复现结果；
- 对照变量和对照结果；
- 与漏洞声明一致或不一致之处；
- Scope 和安全边界；
- 是否生成了新的 `evidence/**`。

新结果具有判定价值时，将最小化、脱敏的原始请求/响应、截图、DOM、网络记录或状态对比写入 `artifacts/evidence/**`。不要保存密钥、完整 Cookie、无关个人数据或大段重复响应。

## 四、四种状态的裁决门槛

### verified

必须同时满足：

- 本 ReviewRun 主动复现成功，或存在无需在线复现即可完整证明的直接静态证据；
- 至少一个关键对照排除了预期行为、缓存、公开数据、客户端假象或错误身份等替代解释；
- CVSS 风格维度中的利用条件和实际 C/I/A 影响有证据支撑；
- 影响对象、前提和范围没有被明显夸大；
- 不是更早漏洞的同根因重复。

提交前阅读：`references/verified-example.md`。

### duplicate

必须同时满足：

- 存在同 Task、更早、未归档的目标漏洞；
- 当前漏洞与更早漏洞共享同一根因、安全边界和主要影响，而非仅标题或 CWE 相似；
- 当前对象确实落入更早漏洞的影响范围；安全可行时应主动验证；
- 说明哪些证据重合、哪些只是不同入口或不同样本；
- 指定合法的 `duplicate_of`。

提交前阅读：`references/duplicate-example.md`。

### ignored

至少满足一种，并有新鲜反证、关键对照或完整直接静态证据：

- 声称现象无法复现，且控制组证明不是环境偶发问题；
- 现象属于公开、预期或已授权行为；
- 没有跨越安全边界，VC/VI/VA 与 SC/SI/SA 均无实际影响；
- 明确超出 Scope 或按 Hint/Knowledge/`whatisvul` 应忽略；
- 原报告把普通错误、版本信息、HTTP 200 或理论可能性误当成漏洞。

“证据少”“请求失败一次”“没有时间验证”都不是 `ignored` 理由；这些通常属于 `needs-human`。

提交前阅读：`references/ignored-example.md`。

### needs-human

适用于关键结论仍未知，且 Reviewer 无法在当前边界内安全解决，例如：

- 缺少第二身份、租户、管理员、设备或业务状态；
- 目标不可达、环境已变化或 Evidence 依赖的状态无法恢复；
- 验证会产生真实扣款、删除、群发、持久化、DoS 或其他不可逆副作用；
- Guard/Scope 明确禁止必要步骤；
- 多个 CVSS 关键维度仍未知，导致真假或实际影响无法裁决。

必须写明已尝试步骤、具体阻塞、未知维度和人类下一步。不要把 `needs-human` 写成泛泛的“建议人工复核”。

提交前阅读：`references/needs-human-example.md`。

## 五、`artifacts/review.md` 标准结构

```markdown
# 漏洞审核记录

## 1. 审核对象与原始声明
## 2. 已读取材料
## 3. 独立验证与关键对照
## 4. CVSS v4.0 风格维度核查
## 5. 实际安全影响与边界
## 6. 历史重复判断
## 7. 裁决与理由
## 8. 不确定性及人工下一步
```

四份附件展示了这个结构在不同裁决下应如何填写。不要省略不适用章节；写“无”并说明原因。

## 六、安全边界

- 严格遵守 Task Scope、Guard 和现有凭证边界。
- 优先重放已有请求并建立单变量对照，禁止扩大扫描、批量枚举或引入新攻击面。
- 禁止破坏性操作、持久化、社工、拒绝服务或越出 Scope 的第三方访问。
- 无法安全验证时停止并选择 `needs-human`，不得为了获得确定结论越界。
- Reviewer 只提交结构化裁决和审核 Artifact；不创建 Fact、Todo 或 Intent，不改写漏洞标题、描述、等级、影响或修复建议。
