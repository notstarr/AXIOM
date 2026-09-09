# explore / reason 上下文消费与产出对账

> 数据口径：`axiom.llm_token_usage` 表真实计量（268,555+ 条记录），非估算。
> 文本长度：全库 facts 7,066 条 / intents 7,121 条 / vulnerabilities 1,023 条实测。
> 每项编号用于逐条对账，批注直接写在文件里或口头对。

---

## 一、EXPLORE worker

### 1.1 吃进去的上下文

- [ ] **E-1 系统提示词**
  - `prompts/explore.md` 6.4KB（≈1.6K tokens）+ `explore_conclude.md` 1.3KB
  - 固定开销，量大但单价低

- [ ] **E-2/E-3 注入头三节：当前执行输入 / 当前目标 /（本轮触发快照）**
  - ① 当前执行输入 = `intent_description`（**reason 写的任务书**，实测均值 579 / p95 1,738 / max 9,549 chars；✗ 写成完整作战计划，每轮 explore 整段进提示）**【用户已确认动刀，见刀③】**
  - ② 当前目标 = `intent_targets`（targets_json，URL/host 靶子清单；t_204 起多为空 → 去重指纹退化）
  - ③ 本轮触发快照 = ReasonRun.trigger_payload_json（仅 reason 有：source_fact_ids / todo_review_ids / open_explore_ids / coalesced_count——"这轮为什么轮到我"）
- [ ] **E-4 注入六索引 —— 用户决策：全部改"目录卡化"（最终形态）**
  - 用户决策 1（2026-09-09）：vulnerabilities / todos / graph **直接移除注入**
  - 用户决策 2：artifacts 索引**不该膨胀**——a/i ID 单调递增，查哪条直接去查，注入只需要"目录卡"
  - 实证支撑：t_197 artifacts.json 1,511 行中 **1,144 行是 evidence 文件逐个登记**（summary=文件名复读，零信息，374KB）——登记粒度设计 bug；findings/leads 每 intent 一对才该上目录
  - **最终形态：六索引全部改"目录卡"**（每张 ~3-5 行：类型学 + 计数 + 查询路径），六张 ≈ 1KB，取代现在最多 1.2MB 的全文内联
  - 目录卡样例（artifacts）：`共 N 件（a001-aNNNN，按 intent 递增）；findings/leads 在 artifacts/<intent_id>/；evidence 同目录 evidence/（review 抽查用）；资产清单在 artifacts/i001/；明细 manifest details/artifact/<hash>.json`
  - 补丁：卡内写明跨 intent 查询的绝对路径前缀（数据目录），worker bash 可达
  - 原始问题存档：全文内联无差别重发（时间维放大）+ 超预算尾部截断致"前期可见后期消失"（t_197 59KB→1.2MB；explore 实际只看到 8% 且 artifacts/whatisvul 整节消失）
- [ ] **E-5 whatisvul 判定标准 —— 用户决策：降级按需读，与普通 skill 一致**
  - 原状：injection.py:184-192 把 SKILL.md + vulnerability-standards.md 两份 markdown 每个 worker **每轮直灌**（固定大头）
  - **已定：删除 whatisvul 特殊注入**；五类 worker（boot/explore/reason/plan/review）统一按普通 skill 按需读
  - 提示词补充"何时读"：explore=上报漏洞前对齐判定标准；review=复核判定时；其余 worker 按需
  - 收益：常量大头直接归零（每轮每 worker 省两份 markdown）

- [ ] **E-6/7/8（合并）按需访问通道——非注入，worker 自助读盘/调工具**
  - E-6 skills 目录（数据目录快照，按需读 SKILL.md）
  - E-7 run_dir 沙箱工作目录：读 bootstrap 产物（jsfinder 等）、写 findings/leads/evidence、sessions 落盘
    - 与 artifacts 数据目录是**两层**：run_dir=工作副本（完成即删，t_204 已实证清空）；data dir=归档副本（长期，跨 intent 查询）
  - E-8 bsk 实时返回（observe/network --detail/credentials/evaluate）——会话内自积累（大块响应体进转录）
  - 目录卡化后此通道成为主信息通道：卡片指路 → worker 按需自助

### 1.2 拉出来的产物

- [ ] **E-O1 facts.description（完成事实）—— 用户决策：只给人看，不给大模型消费**
  - 模板 `_explore_fact`（execution/service.py:1417-1442）；✗ 原设计内嵌 intent 全文（9.5K intent → 9.6K fact）
  - **已定（2026-09-09 对账）：facts.description 全面退出模型消费**
    - 注入侧：graph 索引 facts 行去掉 preview/文本 → 目录卡化后 facts 根本不进注入（代码证据：reason 状态机只用 fact **ID**，从不读文本——service.py:117,455,517,681；唯一 token 消费点就是注入 preview）
    - 写入侧：保留但降级为**人类审计记录**——一行式（`intent:i011 完成；artifacts:a035/a036；vulns:无`），不再内嵌 intent 全文；供 archives/人工考古
  - 实测存档：均值 521-1,690 / max 9,628；~573 条/任务（全为派生文本，零独有信息）

- [ ] **E-O2 vulnerabilities 六字段**
  - title 50 / description 320 / impact 109 / remediation 84 / evidence p95 1,023
  - ✓ 全场最规范
- [ ] **E-O3 artifacts（findings/leads/evidence 登记）**
  - 登记 summary 均值 35 ✓ 指针化正确；t_197 实证 1,144 行 evidence 复读行是登记粒度 bug（刀②同修）
  - 下游：目录卡 → worker 按 storage_key 自助读（E-6/7/8 通道）
- [ ] **E-O3b findings/leads 文件格式 —— 用户定稿模板（提示词规范，刀③落点）**
  - **findings.md**（已确认事实）：
    1. [事实陈述，高效精炼——长度服从内容，讲不清可用多句，但零冗余]
    2. [事实陈述]
    n. ...

    ## 证据清单
    evidence/<file>.txt: <产生动作说明，高效精炼>
    （例：evidence/anon-probe.txt: 匿名 6 连探测（三接口 GET/POST + 401 指纹））
    ```
    证据清单固定在 findings 末尾，一行一文件：`路径: 产生动作`
  - **leads.md**（待验证线索）：一、二、三编号，每条高效精炼（同样长度服从内容）
  - **禁项（两张实拍图的病灶）**：
    - ✗ 跨 intent/版本对账叙事（"已在上游 i150 上报并 verified""按 i153 leads#3 并入 v008、供 td002 复评时扩标题"）——状态同步对话不是事实记录，去 todo/状态字段
    - ✗ 多端点多证据一逗号到底的 jargon 流（"victim 300 confirm → 200 + GET 持久复核落库、victim 219 confirm → 200 next:null、..."）——一事实一行
    - ✗ 请求统计流水（"本轮 23 请求（基线 7 + self 正控 3 + ...总<40）"）——evidence/ 里有，不进结论
  - 下游不变：文件在 artifacts/<intent>/，目录卡指路按需读（E-O3 通道）
- [ ] **E-O4 todos.status_reason（阻塞说明）**
  - 实测 224/573/1,019 —— 偏长；✗ 已定：todos 移除注入（E-4 决策），仅 DB/人工看
- [ ] **E-O5 activity_summary（实时活动卡）**
  - 均值 12 ✓ 代码生成；只进 UI，不进上下文
- [ ] **EXPLORE 合计：162 亿 tokens / 604 个 intent（占全系统 73%）**
  - 单 intent 平均 **26.8M tokens**、约 444 轮调用
  - 单轮：均值 60.4K、p95 155.9K、max 536K
  - 构成：fresh input 4.0K + cache_read 56.3K（93%）+ output 369
  - （pi 内部重放机制你说了不管，故只记一笔：93% 是历史转录重放）

---

## 二、REASON worker
### 2.1 吃进去的上下文

- [ ] **R-1 系统提示词**
  - reason.md 4.3KB + profile 五选一（default/bootstrap/progress/human/continuation，0.9-3.5KB），控制面按 trigger 固定

- [ ] **R-2 注入块：前 3 节 + 六索引 + whatisvul 全文 —— 同步 E-4 决策**
  - wanted 集同步砍：vulnerabilities / todos / **graph** 移除（graph=facts 行集，三重死亡：facts 文本已定"只给人看"、触发快照已给待规划事实 ID 清单、reason 状态机代码只用 fact ID 从不读文本——service.py:117/455/517/681）
  - artifacts：与 explore 同理目录卡化（reason 规划按 storage_key 读各 intent 的 findings/leads）
  - 24K 上限；原 facts 累积放大（同 E-4，随目录卡化消失）

- [ ] **R-3 workspace draft（续跑时重灌）—— 本质是 R-O1/R-O2 病灶的存量化**
  - 是什么：reason 分批规划的"未提交草稿暂存"（reason_workspaces.draft_json），attempt 失败重试时恢复现场继续
  - 构成（t_192 最大样本 60KB）：intent_proposals **44KB/30 条**（= R-O1 膨胀任务书的仓库）· intent_assessments 7KB · todo_updates 6KB（= R-O2 被砍字段的载体）
  - **治理联动**：R-O1（任务书 ≤500）+ R-O2（todo 字段瘦身）落地后 draft 自动缩至 ~15-20KB，无需单独动刀
  - 限额（刀③b 的 8KB）：数值待上游治理落地后实测再定（可放宽至 16-24KB 或 proposals 分批提交）
  - 实测存档：max 61,554 chars —— 六连败 = 6 次全量重灌

- [ ] **R-4 skills / run_dir（按需）**
  - 规划前需读 explore 的 findings/leads artifacts、evidence、manifest details/

### 2.2 拉出来的产物

- [ ] **R-O1 intents.description + targets_json（派生任务书）—— 用户定稿模板（刀③e）**
  - ✗✗✗ 重灾区实锤：t_197/i147（2,162 chars）——背景复述 v007 历史 + 请求级执行方案（发几发/用什么 body/先 GET 再发）+ 跨引用对账（td002/v007/i143/f141 全塞）
  - **任务书模板（reason prompt 规范）**：
    ```
    目标：<测什么——端点/功能/参数 + 漏洞假设>
    判据：<什么算确认，什么算排除>
    前置：<可复用凭据/基线/已知漏洞——只给引用不复述>
    边界：<明确不做的事（可选）>
    ```
  - 核心原则：**reason 只定义"测什么"和"怎么算成"，不定义"怎么做"**；引用不复述；≤500 chars（刀③b）
  - 改写对照：i147 同内容 2,162 → ~200 chars
  - 下游放大环保存档：explore 每轮直灌 → `_explore_fact` 内嵌 → graph 索引（随刀②/③a 消失）
  - targets_json 结构化 ✓（注意填写回归：t_204 起多为空 → 去重指纹退化）

- [ ] **R-O2 todos（reason 产出/更新）—— 用户决策：字段瘦身**
  - **留**：title（均值 34 ✓）+ status_reason（UI 的"描述"，均值 224/p95 573 → 高效精炼软约束）
  - **砍**：priority（自产自销的 LLM 排序信号；消费方 `ORDER BY priority DESC` service.py:1153/2601 → 同步改 fallback created_at 先来先处理）· theme（**278 个去重值**自由文本标签，零聚合价值）· category（10 枚举，无用）
  - 落地：TodoProposal schema 删三字段 + reason prompt 不再让填 + 排序查询改写；旧列保留历史兼容
  - 下游：todos 目录卡（同 E-4 决策）
- [ ] **R-O3 facts（commit 结论事实）—— 同 E-O1 决策：只给人看**
  - 下游原为 graph 索引 → 已随目录卡化退出模型消费；写入降级为人类审计一行式

- [ ] **R-O4 reason_workspaces.draft_json**
  - 下游：**自己**（失败重试时重灌）

- [ ] **R-O5 阶段调度信号（创建 explore intent / 触发下一轮）**
  - 控制流，非上下文

### 2.2 消费体量（真实计量）

- [ ] **REASON 合计：51 亿 tokens（占 23%）**
  - 单轮：均值 45.9K、p95 86.7K、max 201K；每任务 300+ 轮（t_188 = 352 轮）
  - explore + reason = **全系统 96%** —— 刀口选择正确

---

## 三、放大环（问题汇总）

- [ ] **A-1 核心放大环（唯一结构性问题）**
  ```
  reason 写 intent.description（≤9.5K）
    → _explore_fact 内嵌全文 → fact（≤9.6K）
      → graph 索引 → 每轮 reason/explore 重付 ×N
  ```
  实锤：t_150 最长 fact 9,628 vs 最长 intent 9,549，差 79 字符 = 模板包装

- [ ] **A-2 常量肥肉**：whatisvul 全文 + 六索引全文内联，24K/轮 上限常量

- [ ] **A-3 自重灌**：draft_json 61KB 失败重试场景

- [ ] **A-4 根因**：四份 phase prompt 中输出长度约束 **零命中**（grep 精简/简短/字以内/不超过 = 无）

---
- [ ] **刀① session 转录治理** —— **用户裁定：不管**（pi 内部机制不动）。仅存档：93% 消耗是 cache_read（历史转录重放），单 intent 均 26.8M tokens
- [ ] **刀① session 转录治理** —— 治 93% 的 cache_read 大头
  - 工具结果（get-html / bsk network / evaluate 大块输出）截断进转录、分段摘要、轮数控制
  - 预期：explore 单 intent 26.8M → 砍半量级，全库 **~80 亿**
- [ ] **刀② 注入目录卡化**（最终形态，2026-09-09 对账确定）
  - vulnerabilities / todos / graph：移除注入
  - whatisvul 特殊注入删除（E-5 决策：降级按需读，五类 worker 与普通 skill 一致；提示词补"何时读"）
  - hints / knowledge / artifacts：改"目录卡"（3-5 行：类型学+计数+查询路径）
  - artifacts 登记侧同修：evidence 不再逐文件登记（每 intent 聚合或免登记），findings/leads 保留
  - 预期：注入从 24K 上限 → 头三节+卡片 ≈ 2-6K tokens；t_197 类任务从 1.2MB 素材降到恒定 1KB 卡片
  - 风险：低；details/ 溯源与磁盘按需读通道已存在
- [ ] **刀③ 产出精炼**（对账后定稿：fact 降级为人类记录 + intent 限长 + draft 限额）
  - a) `_explore_fact` / reason commit fact 模板：**一行式审计记录**（`intent:i011 完成；artifacts:a035/a036；vulns:无`），不内嵌 intent 全文，不进任何 prompt（**用户已定：fact 只给人看**）
  - b) reason prompt 加硬约束：intent.description ≤500 + 任务书四段式模板（e）；draft ≤8KB
  - c) schema `max_length` 校验（commit 门拒收超限）
  - d) explore prompt 增补 findings/leads 模板纪律（E-O3b 用户定稿：编号事实 + 末尾"## 证据清单"（`路径: 产生动作`一行一文件）；leads 一二三；禁跨 intent 对账叙事/jargon 流/请求流水）
  - e) reason prompt 增补任务书模板（R-O1 用户定稿：目标/判据/前置/边界四段式；只定义"测什么+怎么算成"不定义"怎么做"；引用不复述；≤500 chars）
  - f) reason prompt 增补 assessment_reason 一行式（用户已定：`达成 X/Y；未竟：<原因>；处置：重开/关闭/并入 vX`）——后续规划评审的直接输入
  - 预期：复制源消灭 + graph 索引增速归零；零风险

---

- [x] Q2 whatisvul —— 已定：降级按需读（E-5/刀②）

- [x] Q1 graph 索引 —— 已失效（更优决策：graph 整体移除注入，见 E-4/刀②）
