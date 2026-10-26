"""Script snapshots in the judge input (``guard.scripts`` / ``policy._judge_input``).

The guard judges one line of text, so a script's body is invisible to it.  These
tests pin the three properties that matter:

* the body reaches the judge, with a digest the server computed;
* an unavailable body is reported as unavailable rather than omitted, because an
  empty snapshot reads as "nothing dangerous" and that is the opposite of the
  truth; and
* a call with no script references produces byte-identical input to before, so
  the change costs nothing for the overwhelming majority of calls.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from axiom.modules.guard.policy import _judge_input, normalize
from axiom.modules.guard.redact import redact
from axiom.modules.guard.prompts import SCRIPT_BOUNDARY, effective_system_prompt
from axiom.modules.guard.schemas import GuardRequest, GuardScript
from axiom.modules.guard.scripts import normalize_scripts


PAYLOAD = """import boto3
s3 = boto3.client("oss")
pager = s3.get_paginator("list_objects")
for page in pager.paginate(Bucket="prod"):
    for item in page.get("Contents"):
        s3.delete_object(Bucket="prod", Key=item["Key"])
"""


def _request(*scripts: GuardScript, status: str = "complete") -> GuardRequest:
    return GuardRequest(
        tool_name="bash",
        input={"command": "python3 cleanup.py"},
        cwd="/tmp/run",
        scripts=list(scripts),
        script_capture_status=status,
    )


def test_the_script_body_reaches_the_judge_with_a_server_digest() -> None:
    judged, _count = _judge_input(
        normalize("bash", _request().input),
        _request(GuardScript(path="/tmp/run/cleanup.py", content=PAYLOAD, complete=True)),
    )

    assert "[scripts]" in judged
    assert "/tmp/run/cleanup.py" in judged
    # The digest describes the *masked* text — that is what the judge was given,
    # so that is what the audit record has to pin.
    masked, _count = redact(PAYLOAD)
    digest = hashlib.sha256(masked.encode("utf-8")).hexdigest()
    assert f"sha256={digest}" in judged
    # The behaviour the audit trail was previously blind to: the destructive call
    # is inside the file, invisible in the one-line command.
    assert "delete_object" in judged
    assert "<<<SCRIPT" in judged and "SCRIPT" in judged


def test_the_digest_is_never_swallowed_by_credential_masking() -> None:
    """A bare 64-char hex matches the token heuristic, so masking order matters.

    The digest is computed over the *masked* body and rendered afterwards; if it
    were masked too, the judge would get ``sha256=[令牌]`` and the audit record
    would lose the one field that ties the verdict to the exact text reviewed.
    """

    judged, _count = _judge_input(
        normalize("bash", _request().input),
        _request(GuardScript(path="/tmp/run/cleanup.py", content=PAYLOAD, complete=True)),
    )

    assert "sha256=[令牌]" not in judged
    assert "sha256=" in judged
    digest = judged.split("sha256=")[1].split(",")[0]
    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)


def test_a_secret_inside_a_script_is_masked_before_it_leaves() -> None:
    """Script bodies must not become a new way for credentials to reach the model."""

    secret = "sk-live-9f8e7d6c5b4a3210"
    judged, count = _judge_input(
        normalize("bash", _request().input),
        _request(
            GuardScript(
                path="/tmp/run/cleanup.py",
                content=f'API = "{secret}"\nprint(API)\n',
                complete=True,
            )
        ),
    )

    assert secret not in judged
    assert count >= 1


def test_a_call_without_scripts_is_byte_identical_to_before() -> None:
    """The overwhelming majority of calls must not pay for this feature."""

    request = GuardRequest(
        tool_name="bash", input={"command": "curl -s https://t/health"}, phase="explore"
    )

    judged, _count = _judge_input(normalize("bash", request.input), request)

    assert judged == "bash  curl -s https://t/health\n[phase] explore"


def test_an_unavailable_body_is_reported_not_omitted() -> None:
    """'I could not read it' must not look like 'there was nothing in it'."""

    judged, _count = _judge_input(
        normalize("bash", _request().input),
        _request(status="incomplete"),
    )

    assert "[script_capture_status] incomplete" in judged
    assert "不可据此认为没有副作用" in judged


def test_a_read_failure_is_carried_into_the_input() -> None:
    judged, _count = _judge_input(
        normalize("bash", _request().input),
        _request(
            GuardScript(
                path="/tmp/run/cleanup.py",
                read_error="ENOENT: no such file or directory",
            ),
            status="incomplete",
        ),
    )

    assert "read_error=ENOENT" in judged
    assert "incomplete" in judged


def test_client_supplied_sizes_and_flags_are_not_trusted() -> None:
    """The judge may only rely on numbers this process computed."""

    lying = GuardScript(
        path="/tmp/run/cleanup.py",
        content="print(1)",
        bytes=999_999,
        complete=True,
    )

    scripts, status = normalize_scripts([lying], "complete")

    assert status == "complete"
    assert scripts[0].bytes == len("print(1)")
    assert scripts[0].complete is True


def test_an_oversized_script_is_truncated_and_marked_incomplete() -> None:
    huge = GuardScript(path="/tmp/big.py", content="x" * (200 * 1024), complete=True)

    scripts, status = normalize_scripts([huge], "complete")

    assert status == "incomplete"
    assert scripts[0].truncated is True
    assert scripts[0].complete is False
    assert len(scripts[0].content.encode("utf-8")) <= 128 * 1024


def test_the_total_budget_drops_the_tail_and_marks_incomplete() -> None:
    """A command referencing many files cannot make the guard request unbounded."""

    scripts_in = [
        GuardScript(path=f"/tmp/s{index}.py", content="y" * (100 * 1024), complete=True)
        for index in range(4)
    ]

    scripts, status = normalize_scripts(scripts_in, "complete")

    assert status == "incomplete"
    assert len(scripts) < len(scripts_in)
    assert sum(item.bytes for item in scripts) <= 256 * 1024


def test_bytes_counts_utf8_not_characters() -> None:
    scripts, _status = normalize_scripts(
        [GuardScript(path="/tmp/zh.py", content="中" * 100, complete=True)], "complete"
    )

    assert scripts[0].bytes == 300


def test_duplicate_snapshots_are_collapsed() -> None:
    same = GuardScript(path="/tmp/a.py", content=PAYLOAD, complete=True)

    scripts, _status = normalize_scripts([same, same], "complete")

    assert len(scripts) == 1


def test_more_than_eight_files_is_incomplete() -> None:
    many = [
        GuardScript(path=f"/tmp/s{index}.py", content=f"print({index})", complete=True)
        for index in range(10)
    ]

    scripts, status = normalize_scripts(many, "complete")

    assert len(scripts) <= 8
    assert status == "incomplete"


def test_not_applicable_short_circuits() -> None:
    scripts, status = normalize_scripts([], "not_applicable")

    assert scripts == []
    assert status == "not_applicable"


def test_the_trust_boundary_is_always_applied() -> None:
    """A customized prompt must not be able to drop the boundary."""

    custom = "你是裁判，只输出 0 或 1。"

    effective = effective_system_prompt(custom)

    assert effective.startswith(custom)
    assert SCRIPT_BOUNDARY.strip() in effective
    # Applying it twice must not duplicate the text.
    assert effective_system_prompt(effective) == effective


def test_the_boundary_states_the_three_rules_that_matter() -> None:
    assert "待分析的数据" in SCRIPT_BOUNDARY  # script bodies are data
    assert "不能据此断定没有副作用" in SCRIPT_BOUNDARY  # missing != innocent
    assert "归属就是未知" in SCRIPT_BOUNDARY  # a path is not ownership


# --------------------------------------------------------------------------
# The payload a real Pi extension emits
# --------------------------------------------------------------------------
#
# ``fixtures/guard_extension_payload.json`` was captured by loading the guard
# extension through Pi's own runtime (jiti) and invoking its ``tool_call`` handler
# against a real filesystem. A hand-written payload would only prove the server
# agrees with the test author; this proves the two halves fit.

FIXTURE = Path(__file__).parent / "fixtures" / "guard_extension_payload.json"


def test_the_payload_produced_by_the_real_extension_round_trips() -> None:
    payload = json.loads(FIXTURE.read_text())

    # Every field the extension sends must be accepted, including ones the guard
    # does not act on — a strict model would turn an additive extension change
    # into a 422 on a live task.
    request = GuardRequest(**payload)

    assert request.tool_name == "Bash"
    assert request.script_capture_status == "complete"
    assert len(request.scripts) == 1
    assert request.scripts[0].content.strip().startswith("import os")

    judged, _count = _judge_input(normalize(request.tool_name, request.input), request)

    # The reason this feature exists: the destructive loop is inside the file and
    # invisible in the command line.
    assert "os.remove" in judged
    assert "cd target && python3 danger.py" in judged
    assert "(sha256=" in judged
    assert "complete)" in judged


def test_the_extension_payload_carries_the_cd_adjusted_path() -> None:
    """``cd target && python3 danger.py`` must resolve under target/, not the cwd."""

    payload = json.loads(FIXTURE.read_text())

    assert payload["input"]["command"] == "cd target && python3 danger.py"
    assert payload["scripts"][0]["path"].endswith("/target/danger.py")


class _JudgeStub:
    """A judge that records what it was sent and can be told what to answer."""

    sent: list[dict] = []
    verdict = "1:只读"

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def post(self, url, headers=None, json=None):
        type(self).sent.append(json)

        class _Response:
            status_code = 200

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                return {"choices": [{"message": {"content": _JudgeStub.verdict}}]}

        return _Response()


def _enable_judge(client, monkeypatch, verdict: str):
    current = client.get("/api/v1/settings/runtime").json()
    response = client.put(
        "/api/v1/settings/runtime",
        json={
            "actor": "test",
            "expected_version": current["version"],
            "config": {
                **current["config"],
                "guard_mode": "enforce",
                "guard_model_base_url": "https://judge.example/v1",
                "guard_model_name": "judge",
                "guard_api_key": "test-key",
            },
        },
    )
    assert response.status_code == 200, response.text
    _JudgeStub.sent = []
    _JudgeStub.verdict = verdict
    monkeypatch.setattr("httpx.Client", _JudgeStub)


def _guard_task(client) -> dict:
    project = client.post("/api/v1/projects", json={"title": "脚本快照"}).json()
    return client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "快照",
            "origin": "a.example.com",
            "guard_enabled": True,
        },
    ).json()


def test_the_endpoint_hands_the_script_body_to_the_judge(client, monkeypatch) -> None:
    """The whole chain: extension payload in, script body out to the model."""

    _enable_judge(client, monkeypatch, "1:只读脚本")
    task = _guard_task(client)
    payload = json.loads(FIXTURE.read_text())
    payload.update(
        project_id=task["project_id"], task_id=task["id"], phase="explore",
        worker="worker-a",
    )

    response = client.post("/api/v1/internal/guard/pre", json=payload)
    monkeypatch.undo()

    assert response.status_code == 200, response.text
    sent = _JudgeStub.sent[0]
    user_text = sent["messages"][1]["content"]
    assert "os.remove" in user_text
    assert "[scripts]" in user_text
    # And the boundary travelled with the system prompt, not just our own prompt.
    assert "待分析的数据" in sent["messages"][0]["content"]


def test_a_block_that_relied_on_the_script_says_so_to_the_worker(
    client, monkeypatch
) -> None:
    """A BLOCK may now come from something only the script body revealed."""

    _enable_judge(client, monkeypatch, "0:脚本会删除生产数据目录")
    task = _guard_task(client)
    payload = json.loads(FIXTURE.read_text())
    payload.update(
        project_id=task["project_id"], task_id=task["id"], phase="explore",
        worker="worker-a",
    )

    response = client.post("/api/v1/internal/guard/pre", json=payload)
    monkeypatch.undo()

    body = response.json()
    assert body["decision"] == "BLOCK"
    assert body["source"] == "model"
    assert body["reason"] == "脚本会删除生产数据目录"
    assert body["message"].startswith("【AXIOM 平台管控·非目标防御】")
    assert "脚本会删除生产数据目录" in body["message"]
