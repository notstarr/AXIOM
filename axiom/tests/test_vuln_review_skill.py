from pathlib import Path


SKILL_DIR = (
    Path(__file__).parents[1] / "src" / "axiom" / "dispatcher" / "skills" / "vuln-review"
)

VULN_REPORT_SECTIONS = (
    "## 简述",
    "## 利用前提",
    "## 复现步骤",
    "## 漏洞危害",
    "## 修复建议",
    "## 证据截图",
)


def test_vuln_review_skill_requires_vuln_report_and_status_examples() -> None:
    skill = SKILL_DIR.joinpath("SKILL.md").read_text(encoding="utf-8")

    # CVSS 不再作为产出要求
    assert "不再要求 CVSS 向量" in skill
    # 审核报告已废除，改为每漏洞一份漏洞报告
    assert "review.md" in skill and "已废除" in skill
    assert "vuln_report-<vuln_id>-<slug>.md" in skill
    # 文件名必须纯 ASCII，否则产物会被判非法路径丢弃
    assert "[A-Za-z0-9-]" in skill and "直接丢弃" in skill
    # 「确认成立」才产出报告，其余三态不产出
    assert "duplicate" in skill and "ignored" in skill and "needs-human" in skill
    assert "必须使用 `read` 打开对应附件" in skill

    examples = {
        "verified": "verified-example.md",
        "duplicate": "duplicate-example.md",
        "ignored": "ignored-example.md",
        "needs-human": "needs-human-example.md",
    }
    for decision, filename in examples.items():
        relative = f"references/{filename}"
        assert relative in skill
        example = SKILL_DIR.joinpath(relative).read_text(encoding="utf-8")
        assert f"`{decision}`" in example
        # 示例里不再出现 CVSS 要求
        assert "CVSS" not in example

    # 仅 verified 示例是完整漏洞报告，含固定字段
    verified = SKILL_DIR.joinpath("references/verified-example.md").read_text(
        encoding="utf-8"
    )
    for section in VULN_REPORT_SECTIONS:
        assert section in verified, section
    # 其余三态只是裁决理由示例，明确「不产出报告」
    for filename in ("duplicate-example.md", "ignored-example.md", "needs-human-example.md"):
        text = SKILL_DIR.joinpath("references", filename).read_text(encoding="utf-8")
        assert "不产出报告" in text


def test_vuln_review_status_examples_are_exposed_as_skill_files(client) -> None:
    response = client.get("/api/v1/skills/vuln-review/files")
    assert response.status_code == 200, response.text
    paths = {item["path"] for item in response.json()}

    for filename in (
        "verified-example.md",
        "duplicate-example.md",
        "ignored-example.md",
        "needs-human-example.md",
    ):
        relative = f"references/{filename}"
        assert relative in paths
        content = client.get(f"/api/v1/skills/vuln-review/files/{relative}")
        assert content.status_code == 200, content.text
        body = content.json()["content"]
        # 旧的 CVSS 审核记录结构已废弃
        assert "CVSS v4.0 风格维度核查" not in body
        assert "review.md" not in body or "已废除" in body
