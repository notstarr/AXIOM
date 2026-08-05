from pathlib import Path


SKILL_DIR = (
    Path(__file__).parents[1] / "src" / "axiom" / "dispatcher" / "skills" / "vuln-review"
)


def test_vuln_review_skill_uses_cvss_dimensions_and_requires_status_examples() -> None:
    skill = SKILL_DIR.joinpath("SKILL.md").read_text(encoding="utf-8")

    assert "CVSS v4.0" in skill
    assert "Attack Vector（AV）" in skill
    assert "Attack Requirements（AT）" in skill
    assert "Vulnerable System Confidentiality（VC）" in skill
    assert "Subsequent System Integrity（SI）" in skill
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
        assert "## 3. 独立验证与关键对照" in example
        assert "## 4. CVSS v4.0 风格维度核查" in example
        assert "## 7. 裁决与理由" in example


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
        assert "CVSS v4.0 风格维度核查" in content.json()["content"]
