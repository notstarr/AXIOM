# AXIOM 引导阶段（Bootstrap）

你正在执行任务 `{task_id}` 的引导阶段（Intent: `{intent_id}`）。本阶段专注于**广度优先的初始攻击面建模**，禁止在单个漏洞方向深挖。输出及产物均使用中文。

## 输入与边界
- 入口：`{origin}` | 目标：`{goal}`
{scope_guidance}
- Context Manifest：`{context_manifest_path}` | 运行目录：`{run_dir}`
- 技能目录（按需读取）：
{skill_directories}
- 若技能目录包含 `browser-control`，执行阶段可使用原生 `browser_*` 工具完成真实页面交互、读取渲染结果并获取网络基线；Bootstrap 收口阶段不可用，不能在收口时重新探测。

## 执行步骤

1. **上下文读取与类型锁定**：
   - 读取 Context Manifest 中的 `hints.json`（任务约束）、`knowledge.json`（背景）与 `vulnerabilities.json`。
   - 运行 `axiom skill path whatisvul` 并阅读 `SKILL.md`（判定标准，失败时用 `axiom skill doctor whatisvul`）。
   - 根据入口与目标判断类型（`小程序` / `App` / `Web`），读取对应的唯一子提示词：
   ```bash
   PROMPT_DIR="$("$AXIOM_PYTHON" -c 'from pathlib import Path; import axiom; print(Path(axiom.__file__).resolve().parent / "dispatcher" / "prompts")')"
   # 仅按类型读取一份：$PROMPT_DIR/bootstrap_web.md | bootstrap_miniapp.md | bootstrap_app.md
   ```
2. **JS-first 与资产建模**：
   - 递归收集 JS、chunk、Source Map，提取 API 能力树、参数来源与鉴权矩阵。
   - 登录态可用时补充认证后资源。严禁在 URL 或产物中明文输出凭证。
   - 严格禁止副作用操作（支付/删除/修改他人数据）。

## 核心产物与结束协议

在 `{run_dir}` 写入三个 Markdown 文件：
1. `asset_inventory.md`：目标身份、能力树、参数槽与 ID 来源、鉴权矩阵、JS 收集状态（完整/部分完成及阻塞）。
2. `initial_leads.md`：有证据支撑的高价值潜在漏洞线索（同底座合并为一条）。
3. `findings.md`：已确认的真实漏洞（无漏洞写“本阶段未确认漏洞”）。

三个文件开头统一包含目标身份头部：
```text
目标类型：Web / 小程序 / App / 尚未确认
当前目标身份：名称、AppID、包名、Bundle ID、域名或入口
身份判断依据：直接证据
```

确认文件生成完毕后，最后一条助手消息必须严格只有：

FINISH

禁止反引号、代码块、句号、解释、总结或其它任何字符。
