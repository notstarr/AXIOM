from __future__ import annotations

from dataclasses import dataclass


PHASES = ("bootstrap", "reason", "explore")
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
    )


BUILTIN_TOOLS: tuple[BuiltinToolDefinition, ...] = (
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
        key="axiom.reason.intent.propose",
        title="派发 Explore Intent",
        description="在当前 Reason Workspace 中提出新的 Explore Intent。",
        source="axiom_internal",
        invocation="axiom reason intent propose ...",
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
        key="axiom.explore.vulnerability.report",
        title="上报漏洞",
        description="在活动 Explore Lease 中上报已确认漏洞及其证据。",
        source="axiom_internal",
        invocation="axiom explore vulnerability report ...",
        availability=_all_modes("explore"),
    ),
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
