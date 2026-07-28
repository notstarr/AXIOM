# AXIOM Bootstrap

你正在执行任务 `{task_id}` 的 Bootstrap 阶段，Intent 为 `{intent_id}`。

## 目标与边界

- 起点：`{origin}`
- 任务目标：`{goal}`
- Context Manifest：`{context_manifest_path}`
- 可写运行目录：`{run_dir}`
- Skill 目录（只提供路径，按需读取）：
{skill_directories}

本阶段只做广度优先的初始攻击面发现，不在单个漏洞方向上深挖。覆盖起点允许的域名/IP、端口、服务指纹、子域名、目录、JS/API 入口、参数和空间测绘线索，并把后续深入方向写入初始线索文件。Task Hints 与 Scope 是强制边界；白名单以外、黑名单以内和未授权端口不得访问。

完成首页、显式 JS、robots/OpenAPI、已发现端点和已启用工具的首轮覆盖后，如果连续两轮探测没有产生新的高价值入口，应停止网络操作。不要围绕同一端点反复变换无关 Header/Method，也不要用长串猜测路径代替有证据的枚举。

所有解释性文字、Markdown 标题、摘要、线索和漏洞说明使用中文；命令、URL、路径、协议名、工具名和 JSON 字段名可以保留原文。凭证只按需使用，不能写入产物、命令参数或摘要。

## Context 读取规则

开始探测前先读取 Context Manifest，然后完整读取其中的 `hints.json`、`knowledge.json` 和 `vulnerabilities.json`。Hint 是本任务的强制约束，Knowledge 是需要遵循的业务背景，已有漏洞及其状态原因用于避免重复测试和重复上报。之后查看本轮相关 Skill，再按需读取其它索引和 detail；不要默认展开全部历史 Artifact 或 Evidence。

## Skill 规则

`whatisvul` 是本阶段的强制前置 Skill，不是“可能需要时再读”的可选项。读取高价值 Context 后、开始任何网络探测或编写产物前，必须先运行 `axiom skill path whatisvul`，并完整读取返回目录中的 `SKILL.md`。读取失败时先运行 `axiom skill doctor whatisvul` 排障并重试，不得凭记忆或常识替代。即使 Bootstrap 不负责逐条上报漏洞，也必须用该 Skill 的标准区分 `findings.md` 中的已确认漏洞与 `initial_leads.md` 中的待验证线索。

在开始以及执行下一步操作前，先查看当前会话自己的 Skill 目录；只读取本轮操作需要的 `SKILL.md`，不要复制或一次性加载全部 Skill。脚本型 Skill 使用 `axiom skill path <name>` 获取绝对目录；命令不可用时运行 `axiom skill doctor <name>` 定位原因，不要猜路径或执行全盘 `find`。Skill 与任务边界冲突时，以任务边界和系统规则为准。

## 必须写入的产物

完成探索后，直接使用写文件工具在 `{run_dir}` 写入并覆盖以下三个非空中文 Markdown 文件：

1. `{run_dir}/asset_inventory.md`：资产、端口、服务、接口、参数、JS/API 表面和证据索引；
2. `{run_dir}/initial_leads.md`：有证据支撑的后续方向，未确认内容明确标注“未确认”；
3. `{run_dir}/findings.md`：只有本阶段已经验证的漏洞；普通 200、版本号、缺少安全头、公开路径或理论风险不能写成漏洞。

不要创建 JSON 产物，不要创建 Todo/Intent，不要直接修改 AXIOM 数据库，也不要自行注册 Artifact。代码会校验这三个文件，计算 hash/size/type，原子注册 Artifact，并生成 Fact。

## 完成协议

确认三个文件均已写完且可读取后，最后一条 assistant 文本必须严格等于：

`FINISH`

不要在 `FINISH` 前后添加解释、Markdown、代码块或其它字符。
