from __future__ import annotations

"""复测方法论 Skill 的存在性与边界。

这份 SKILL.md 是 Reviewer 做复测时唯一的判断依据，所以真正要守的不是「文件在」，
而是「它教的东西和审核不同」：三态、证据下限、账号基线与写操作边界必须齐备，而
审核那套裁决词汇一个都不能出现——同一份内容被复测 run 读到，模型就会开始定档。
"""

BANNED = ("verified", "duplicate", "ignored", "needs-human")


def test_vuln_retest_skill_is_bundled_and_keeps_the_three_state_vocabulary(client) -> None:
    listed = client.get("/api/v1/skills")
    assert listed.status_code == 200, listed.text
    names = {item["name"] for item in listed.json()}
    assert "vuln-retest" in names

    skill = client.get("/api/v1/skills/vuln-retest").json()
    assert skill["source"] == "bundled"
    content = skill["content"]

    for heading in (
        "三态判定门槛",
        "证据下限",
        "账号基线重建",
        "写操作边界",
        "提交前自查",
    ):
        assert heading in content, heading

    for verdict in ("exists", "fixed", "inconclusive"):
        assert verdict in content, verdict

    # 复测不是审核：裁决词汇不该出现在这里
    for banned in BANNED:
        assert banned not in content, banned


def test_vuln_retest_skill_names_the_traps_that_used_to_be_misjudged(client) -> None:
    """最容易被误判成「已修复」的几种情况，必须白纸黑字写下来。"""
    content = client.get("/api/v1/skills/vuln-retest").json()["content"]

    # 打不通 ≠ 修好了
    assert "inconclusive" in content
    assert "超时" in content
    assert "WAF" in content
    # 用管理员身份验证会把「本来就能做」当成漏洞还在
    assert "管理员" in content
    # 复测只截关键那一张，不逐步举证
    assert "至少 1 张" in content
