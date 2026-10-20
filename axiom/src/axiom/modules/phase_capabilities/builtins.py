from __future__ import annotations

from dataclasses import dataclass


PHASES = ("bootstrap", "reason", "explore", "plan", "review")
RUN_MODES = ("execute", "conclude")


@dataclass(frozen=True, slots=True)
class BuiltinToolDefinition:
    """One model-facing capability whose assignment is fixed by AXIOM code."""

    key: str
    title: str
    description: str
    source: str
    invocation: str
    availability: tuple[tuple[str, tuple[str, ...]], ...]

    @property
    def phases(self) -> tuple[str, ...]:
        available = {phase for phase, modes in self.availability if modes}
        return tuple(phase for phase in PHASES if phase in available)

    def modes_for(self, phase: str) -> tuple[str, ...]:
        return next((modes for item_phase, modes in self.availability if item_phase == phase), ())

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "source": self.source,
            "invocation": self.invocation,
            "phases": list(self.phases),
            "availability": {
                phase: list(self.modes_for(phase)) for phase in self.phases
            },
            "locked": True,
        }


def _all_modes(*phases: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    selected = set(phases)
    return tuple(
        (phase, RUN_MODES if phase in selected else ())
        for phase in PHASES
    )


def _bootstrap_execute_else_all() -> tuple[tuple[str, tuple[str, ...]], ...]:
    return (
        ("bootstrap", ("execute",)),
        ("reason", RUN_MODES),
        ("explore", RUN_MODES),
        ("plan", RUN_MODES),
        ("review", RUN_MODES),
    )


_CORE_BUILTIN_TOOLS: tuple[BuiltinToolDefinition, ...] = (
    BuiltinToolDefinition(
        key="read",
        title="读取文件",
        description="读取当前运行目录和已披露 Context 中的文件。",
        source="pi_native",
        invocation="read",
        availability=_all_modes(*PHASES),
    ),
    BuiltinToolDefinition(
        key="write",
        title="写入文件",
        description="创建或覆盖当前阶段合同允许的文件。",
        source="pi_native",
        invocation="write",
        availability=_all_modes(*PHASES),
    ),
    BuiltinToolDefinition(
        key="edit",
        title="编辑文件",
        description="对当前运行目录中的既有文件进行局部修改。",
        source="pi_native",
        invocation="edit",
        availability=_all_modes(*PHASES),
    ),
    BuiltinToolDefinition(
        key="bash",
        title="执行 Shell",
        description="执行阶段合同允许的命令；Bootstrap 收口阶段固定禁用。",
        source="pi_native",
        invocation="bash",
        availability=_bootstrap_execute_else_all(),
    ),
    BuiltinToolDefinition(
        key="web_search",
        title="互联网检索",
        description="通过 DuckDuckGo / Exa 搜索引擎检索公开漏洞库、CVE 细节与最新利用方案。",
        source="pi_native",
        invocation="web_search",
        availability=_all_modes("reason", "explore", "plan"),
    ),
    BuiltinToolDefinition(
        key="fetch_content",
        title="提取网页正文",
        description="抓取外部网页/文档并将 HTML 自动转换为 Markdown 正文。",
        source="pi_native",
        invocation="fetch_content",
        availability=_all_modes("reason", "explore", "plan"),
    ),
    BuiltinToolDefinition(
        key="axiom.skill.path",
        title="定位 Skill",
        description="解析本 Lease 已启用 Skill 的受控绝对目录。",
        source="axiom_internal",
        invocation="axiom skill path <name>",
        availability=_bootstrap_execute_else_all(),
    ),
    BuiltinToolDefinition(
        key="axiom.skill.doctor",
        title="检查 Skill",
        description="检查本 Lease Skill 的文件、命令、依赖和探针。",
        source="axiom_internal",
        invocation="axiom skill doctor [name]",
        availability=_bootstrap_execute_else_all(),
    ),
    BuiltinToolDefinition(
        key="axiom.credential.list",
        title="列出项目凭证",
        description="通过当前活动 Lease 获取可用凭证摘要。",
        source="axiom_internal",
        invocation="axiom credential list",
        availability=_bootstrap_execute_else_all(),
    ),
    BuiltinToolDefinition(
        key="axiom.credential.get",
        title="读取项目凭证",
        description="把指定项目凭证安全写入当前运行目录。",
        source="axiom_internal",
        invocation="axiom credential get <id> --output <path>",
        availability=_bootstrap_execute_else_all(),
    ),
    BuiltinToolDefinition(
        key="axiom.credential.refresh",
        title="读取凭证刷新说明",
        description="获取当前项目凭证的受控刷新材料。",
        source="axiom_internal",
        invocation="axiom credential refresh <id> --output <path>",
        availability=_bootstrap_execute_else_all(),
    ),
    BuiltinToolDefinition(
        key="axiom.reason.status",
        title="读取 Reason 工作空间",
        description="读取本轮 Reason Workspace、额度和必须处理的增量输入。",
        source="axiom_internal",
        invocation="axiom reason status",
        availability=_all_modes("reason"),
    ),
    BuiltinToolDefinition(
        key="axiom.reason.todo.add",
        title="新增 Todo",
        description="向当前 Reason Workspace 新增一个待办。",
        source="axiom_internal",
        invocation="axiom reason todo add ...",
        availability=_all_modes("reason"),
    ),
    BuiltinToolDefinition(
        key="axiom.reason.todo.update",
        title="更新 Todo",
        description="更新已有 Todo 的状态、原因和 Fact 来源。",
        source="axiom_internal",
        invocation="axiom reason todo update ...",
        availability=_all_modes("reason"),
    ),
    BuiltinToolDefinition(
        key="axiom.reason.todo.delete",
        title="删除/归档 Todo",
        description="删除工作区草稿待办或归档重复/无意义的已有 Todo。",
        source="axiom_internal",
        invocation="axiom reason todo delete --todo-id ... --reason ...",
        availability=_all_modes("reason"),
    ),
    BuiltinToolDefinition(
        key="axiom.reason.intent.propose",
        title="派发 Explore Intent",
        description="在当前 Reason Workspace 中提出新的 Explore Intent。",
        source="axiom_internal",
        invocation="axiom reason intent propose ...",
        availability=_all_modes("reason"),
    ),
    BuiltinToolDefinition(
        key="axiom.reason.intent.assess",
        title="评估 Explore Intent",
        description="对已结束且尚未评估的 Explore Intent 做一次性状态评估。",
        source="axiom_internal",
        invocation="axiom reason intent assess --intent-id ... --status ... --reason ...",
        availability=_all_modes("reason"),
    ),
    BuiltinToolDefinition(
        key="axiom.reason.goal.assess",
        title="评估当前 Goal",
        description="提交当前 Goal 是否达成及其证据引用。",
        source="axiom_internal",
        invocation="axiom reason goal assess ...",
        availability=_all_modes("reason"),
    ),
    BuiltinToolDefinition(
        key="axiom.plan.status",
        title="读取 Plan 工作空间",
        description="读取本轮独立 Plan Workspace、全景输入快照和提交就绪状态。",
        source="axiom_internal",
        invocation="axiom plan status",
        availability=_all_modes("plan"),
    ),
    BuiltinToolDefinition(
        key="axiom.plan.todo.add",
        title="Plan 新增 Todo",
        description="在当前 Plan Workspace 中新增一个有全局证据来源的待办。",
        source="axiom_internal",
        invocation="axiom plan todo add ...",
        availability=_all_modes("plan"),
    ),
    BuiltinToolDefinition(
        key="axiom.plan.todo.update",
        title="Plan 更新 Todo",
        description="复核并更新历史 Todo 的状态、理由和来源。",
        source="axiom_internal",
        invocation="axiom plan todo update ...",
        availability=_all_modes("plan"),
    ),
    BuiltinToolDefinition(
        key="axiom.plan.todo.delete",
        title="Plan 归档 Todo",
        description="删除 Plan 草稿 Todo，或以 obsolete 语义归档既有 Todo。",
        source="axiom_internal",
        invocation="axiom plan todo delete --todo-id ... --reason ...",
        availability=_all_modes("plan"),
    ),
    BuiltinToolDefinition(
        key="axiom.plan.intent.propose",
        title="Plan 派发 Explore Intent",
        description="从全景复盘结果提出带来源、去重签名和停止条件的新 Explore Intent。",
        source="axiom_internal",
        invocation="axiom plan intent propose ...",
        availability=_all_modes("plan"),
    ),
    BuiltinToolDefinition(
        key="axiom.plan.decision.submit",
        title="提交 Plan 决策",
        description="提交 continue、needs_reason、dispatch_explore 或 exhausted 决策。",
        source="axiom_internal",
        invocation="axiom plan decision submit ...",
        availability=_all_modes("plan"),
    ),
    BuiltinToolDefinition(
        key="axiom.plan.exhaustion.submit",
        title="提交 Plan 穷尽证明",
        description="提交审查维度、穷尽理由、证据引用和置信度。",
        source="axiom_internal",
        invocation="axiom plan exhaustion submit --reason ...",
        availability=_all_modes("plan"),
    ),
    BuiltinToolDefinition(
        key="axiom.explore.vulnerability.report",
        title="上报漏洞",
        description="在活动 Explore Lease 中上报已确认漏洞及其证据。",
        source="axiom_internal",
        invocation="axiom explore vulnerability report ...",
        availability=_all_modes("explore"),
    ),
    BuiltinToolDefinition(
        key="axiom.explore.result.report",
        title="上报红队成果",
        description="在当前 Explore Chain 上报权限、数据、凭证或横向控制成果。",
        source="axiom_internal",
        invocation="axiom explore result report --type ...",
        availability=_all_modes("explore"),
    ),
    BuiltinToolDefinition(
        key="axiom.explore.human_intervention.mark",
        title="需要人工介入",
        description="标记当前 Explore Intent 中必须由人类完成才能继续验证的方向。",
        source="axiom_internal",
        invocation="axiom explore human-intervention mark --reason ...",
        availability=_all_modes("explore"),
    ),
    BuiltinToolDefinition(
        key="axiom.review.status",
        title="读取漏洞审核状态",
        description="读取当前 ReviewRun、目标漏洞和结构化裁决状态。",
        source="axiom_internal",
        invocation="axiom review status",
        availability=_all_modes("review"),
    ),
    BuiltinToolDefinition(
        key="axiom.review.decision.submit",
        title="提交漏洞审核裁决",
        description="提交已验证、重复、忽略或继续待验证的结构化裁决。",
        source="axiom_internal",
        invocation="axiom review decision submit ...",
        availability=_all_modes("review"),
    ),
)


# Browser Control is a fixed AXIOM Extension, not a managed package.  The
# browser-control Skill only documents these tools; the Broker owns Chrome
# startup, Lease isolation, network capture and cleanup.
BROWSER_BUILTIN_TOOL_NAMES = (
    "browser_open_page",
    "browser_list_pages",
    "browser_close_page",
    "browser_navigate",
    "browser_read_page",
    "browser_snapshot",
    "browser_click",
    "browser_fill",
    "browser_press",
    "browser_hover",
    "browser_scroll",
    "browser_select",
    "browser_check",
    "browser_upload_file",
    "browser_wait",
    "browser_screenshot",
    "browser_read_network",
    "browser_read_console",
    "browser_evaluate",
    "browser_get_credentials",
)

_BROWSER_TOOL_TITLES = {
    "browser_open_page": "打开页面",
    "browser_list_pages": "列出页面",
    "browser_close_page": "关闭页面",
    "browser_navigate": "页面导航",
    "browser_read_page": "读取页面",
    "browser_snapshot": "页面快照",
    "browser_click": "点击元素",
    "browser_fill": "填写输入",
    "browser_press": "发送按键",
    "browser_hover": "悬停元素",
    "browser_scroll": "滚动页面",
    "browser_select": "选择下拉项",
    "browser_check": "切换复选框",
    "browser_upload_file": "上传文件",
    "browser_wait": "等待页面",
    "browser_screenshot": "页面截图",
    "browser_read_network": "读取页面流量",
    "browser_read_console": "读取控制台",
    "browser_evaluate": "检查页面脚本",
    "browser_get_credentials": "提取凭证与Cookie",
}

BROWSER_BUILTIN_TOOLS: tuple[BuiltinToolDefinition, ...] = tuple(
    BuiltinToolDefinition(
        key=name,
        title=_BROWSER_TOOL_TITLES[name],
        description=(
            "通过当前阶段 Browser Lease 操作受控 Chrome 页面；"
            "仅能访问任务 Scope 允许的网络目标。"
        ),
        source="axiom_browser",
        invocation=name,
        # Bootstrap uses the browser during its execute pass to establish a
        # real baseline.  Its conclude pass is deliberately kept artifact-
        # only, matching the Bootstrap conclude contract.
        availability=(
            ("bootstrap", ("execute",)),
            ("reason", ()),
            ("explore", RUN_MODES),
            ("plan", ()),
            ("review", ()),
        ),
    )
    for name in BROWSER_BUILTIN_TOOL_NAMES
)

BUILTIN_TOOLS: tuple[BuiltinToolDefinition, ...] = (
    *_CORE_BUILTIN_TOOLS,
    *BROWSER_BUILTIN_TOOLS,
)


def builtin_tool_payloads(*, phase: str | None = None) -> list[dict[str, object]]:
    if phase is not None and phase not in PHASES:
        raise ValueError(f"invalid phase: {phase}")
    return [
        item.as_dict()
        for item in BUILTIN_TOOLS
        if phase is None or phase in item.phases
    ]


def native_tool_names(phase: str, mode: str) -> list[str]:
    if phase not in PHASES:
        raise ValueError(f"invalid phase: {phase}")
    if mode not in RUN_MODES:
        raise ValueError(f"invalid run mode: {mode}")
    return [
        item.key
        for item in BUILTIN_TOOLS
        if item.source == "pi_native" and mode in item.modes_for(phase)
    ]
