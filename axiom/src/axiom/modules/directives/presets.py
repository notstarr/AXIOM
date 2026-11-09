"""预置指令模板。

预设只是录入捷径：前端选中后把 ``{placeholder}`` 替换成用户填的值，落库的仍是
替换后的最终文本。注入链路不认识预设，也不需要认识——这样以后加模板不用动
dispatcher、不涉及迁移，也不会出现「预设改了但历史指令跟着变」的问题。
"""

from __future__ import annotations

from pydantic import BaseModel


class DirectivePreset(BaseModel):
    key: str
    scope: str
    label: str
    template: str
    placeholder: str | None = None
    hint: str = ""
    example: str = ""


PRESETS: list[DirectivePreset] = [
    DirectivePreset(
        key="worker_forbid_target",
        scope="all_workers",
        label="禁止测试指定资产",
        template=(
            "不允许测试 {target}。不得对其发起任何请求、扫描或探测；"
            "确需触碰时先停下来问，不要自行判断「这只是只读」。"
        ),
        placeholder="target",
        hint="填域名或资产，例如 pay.example.com",
        example="pay.example.com",
    ),
    DirectivePreset(
        key="explore_mark_traces",
        scope="explore_all",
        label="痕迹统一标记",
        template=(
            "新增或编辑的内容、留下的痕迹统一用「{marker}」加当前 Intent 编号命名"
            "（例如 SecPentest-i007），便于事后识别与清理。"
        ),
        placeholder="marker",
        hint="填标记前缀，后面会自动拼 Intent 编号",
        example="SecPentest-",
    ),
    DirectivePreset(
        key="reason_no_authz",
        scope="reason_all",
        label="不派发越权类测试",
        template=(
            "不派发越权类测试：水平越权、垂直越权、IDOR、BOLA、"
            "对象级所有权校验绕过，均不安排。"
        ),
    ),
]
