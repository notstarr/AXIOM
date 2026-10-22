from __future__ import annotations

"""The judge request must not carry credentials or personal data.

These tests are the contract for ``guard.redact``: every value that would be a
leak in a log line is replaced, every value that is evidence for the verdict
(IPs, ports, MACs, paths, ids) is left alone, and the shape of the command
survives both.
"""

import json

import pytest

from axiom.modules.guard.policy import _judge_input, _model_decision, evaluate, normalize
from axiom.modules.guard.redact import (
    PLACEHOLDER_ADDRESS,
    PLACEHOLDER_BANK_CARD,
    PLACEHOLDER_COOKIE,
    PLACEHOLDER_EMAIL,
    PLACEHOLDER_ID_CARD,
    PLACEHOLDER_KEY,
    PLACEHOLDER_NAME,
    PLACEHOLDER_PASSWORD,
    PLACEHOLDER_PHONE,
    PLACEHOLDER_PRIVATE_KEY,
    PLACEHOLDER_TOKEN,
    _is_id_card,
    _is_luhn,
    redact,
)
from axiom.modules.guard.schemas import GuardRequest
from axiom.modules.settings.schemas import RuntimeConfig


SECRET = "Xy9!Tr0ub4dor"
VALID_ID_CARD = "110101199003071233"
VALID_BANK_CARD = "4111111111111111"


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # curl basic auth
        (f"curl -u admin:{SECRET} https://t/login", f"curl -u admin:{PLACEHOLDER_PASSWORD} https://t/login"),
        # url userinfo, including an unencoded @ inside the password
        (
            f"psql 'postgres://app:{SECRET}@10.0.0.5:5432/orders'",
            f"psql 'postgres://app:{PLACEHOLDER_PASSWORD}@10.0.0.5:5432/orders'",
        ),
        (
            "psql 'postgres://app:p@ss@10.0.0.5/db'",
            f"psql 'postgres://app:{PLACEHOLDER_PASSWORD}@10.0.0.5/db'",
        ),
        # bearer token keeps the scheme word
        (
            "curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop' https://t/me",
            f"curl -H 'Authorization: Bearer {PLACEHOLDER_TOKEN}' https://t/me",
        ),
        (
            "curl -H 'Authorization: Basic dXNlcjpwYXNz' https://t/me",
            f"curl -H 'Authorization: Basic {PLACEHOLDER_TOKEN}' https://t/me",
        ),
        # cookies, including a multi-pair value
        (
            'curl -H "Cookie: JSESSIONID=ABC123; uid=7" https://t/x',
            f'curl -H "Cookie: {PLACEHOLDER_COOKIE}" https://t/x',
        ),
        (
            'curl -H "X-Api-Key: AKIDabcdefghijklmn" https://t/x',
            f'curl -H "X-Api-Key: {PLACEHOLDER_TOKEN}" https://t/x',
        ),
        # json body
        (
            "curl -d '{\"username\":\"admin\",\"password\":\"" + SECRET + "\"}' https://t/login",
            'curl -d \'{"username":"admin","password":"' + PLACEHOLDER_PASSWORD + '"}\' https://t/login',
        ),
        # form body
        (
            f'curl -d "username=admin&pwd={SECRET}" https://t/login',
            f'curl -d "username=admin&pwd={PLACEHOLDER_PASSWORD}" https://t/login',
        ),
        # database clients: -p is a password here
        (f"mysql -h 10.0.0.5 -uroot -p{SECRET} orders", f"mysql -h 10.0.0.5 -uroot -p{PLACEHOLDER_PASSWORD} orders"),
        (
            f"redis-cli -h 10.0.0.5 -a {SECRET} ping",
            f"redis-cli -h 10.0.0.5 -a {PLACEHOLDER_PASSWORD} ping",
        ),
        (
            f"sshpass -p {SECRET} ssh root@10.0.0.5",
            f"sshpass -p {PLACEHOLDER_PASSWORD} ssh root@10.0.0.5",
        ),
        # smbclient keeps the account, drops the secret after the %
        (
            f"smbclient //10.0.0.5/share -U CORP/alice%{SECRET}",
            f"smbclient //10.0.0.5/share -U CORP/alice%{PLACEHOLDER_PASSWORD}",
        ),
        # long flags
        (f"curl --password={SECRET} https://t/x", f"curl --password={PLACEHOLDER_PASSWORD} https://t/x"),
        # environment assignments
        (
            "export AWS_SECRET_ACCESS_KEY=wJalrXVnFFEEMI/K7MDENG",
            f"export AWS_SECRET_ACCESS_KEY={PLACEHOLDER_KEY}",
        ),
        (f"PGPASSWORD={SECRET} psql -h db -c 'select 1'", f"PGPASSWORD={PLACEHOLDER_PASSWORD} psql -h db -c 'select 1'"),
        # a private key block collapses to one marker
        (
            "printf '%s' '-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----' > k.pem",
            f"printf '%s' '{PLACEHOLDER_PRIVATE_KEY}' > k.pem",
        ),
        # The flag's own name is the secret word - the shape the first version
        # of the flag rule silently skipped.
        ("curl --password=hunter2 https://t/x", f"curl --password={PLACEHOLDER_PASSWORD} https://t/x"),
        ("curl --password hunter2 https://t/x", f"curl --password {PLACEHOLDER_PASSWORD} https://t/x"),
        # -b / --cookie hand the cookie as a value, not as a header
        ('curl -b "sid=abc" https://t/x', f'curl -b "{PLACEHOLDER_COOKIE}" https://t/x'),
        ('curl --cookie "sid=abc" https://t/x', f'curl --cookie "{PLACEHOLDER_COOKIE}" https://t/x'),
    ],
)
def test_credentials_are_masked_and_their_shape_is_kept(command: str, expected: str) -> None:
    masked, count = redact(command)
    assert masked == expected
    assert count >= 1
    assert SECRET not in masked


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # Keys are matched on the secret word rather than the whole name,
        # because real payloads spell it loginPwd / passWord / user_password.
        (
            'curl -d \'{"loginName":"admin","loginPwd":"abc"}\' https://t/login',
            'curl -d \'{"loginName":"admin","loginPwd":"' + PLACEHOLDER_PASSWORD + '"}\' https://t/login',
        ),
        (
            'curl -d \'{"passWord":"abc"}\' https://t/login',
            'curl -d \'{"passWord":"' + PLACEHOLDER_PASSWORD + '"}\' https://t/login',
        ),
        (
            'curl -d \'{"user_password":"abc"}\' https://t/login',
            'curl -d \'{"user_password":"' + PLACEHOLDER_PASSWORD + '"}\' https://t/login',
        ),
        # The closing quote of a JSON key sits between name and colon, so a
        # header written as JSON was missed until the rule allowed it.
        (
            'curl -d \'{"headers":{"Authorization":"Bearer abc123"}}\' https://t/api',
            'curl -d \'{"headers":{"Authorization":"Bearer ' + PLACEHOLDER_TOKEN + '"}}\' https://t/api',
        ),
        (
            'curl -d \'{"Cookie":"JSESSIONID=abc"}\' https://t/api',
            'curl -d \'{"Cookie":"' + PLACEHOLDER_COOKIE + '"}\' https://t/api',
        ),
        # Chinese field names
        ('curl -d "密码=abc" https://t/login', f'curl -d "密码={PLACEHOLDER_PASSWORD}" https://t/login'),
        ('curl -d "口令=abc" https://t/login', f'curl -d "口令={PLACEHOLDER_PASSWORD}" https://t/login'),
        ('curl -d "令牌=abc" https://t/login', f'curl -d "令牌={PLACEHOLDER_TOKEN}" https://t/login'),
    ],
)
def test_keys_are_recognised_in_the_shapes_real_payloads_use(command: str, expected: str) -> None:
    assert redact(command)[0] == expected


@pytest.mark.parametrize(
    "secret",
    [
        "hunter2",
        "S3cr3t-P@ss",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop",
        "AKIDabcdefghijklmn",
        "wJalrXVnFFEEMI/K7MDENG",
        VALID_ID_CARD,
        "13800138000",
        "alice@corp.example",
    ],
)
def test_no_value_survives_in_the_judged_text(secret: str) -> None:
    # Every slot carries a key, because a value with no key is the one case this
    # module deliberately does not touch - masking it would also mask the SQLi
    # and deserialisation payloads the judge has to read verbatim.
    command = (
        f"curl -u admin:{secret} -H 'X-Api-Key: {secret}' "
        f"-d 'password={secret}' 'https://t/login?token={secret}'"
    )
    masked, _ = redact(command)
    assert secret not in masked
    assert PLACEHOLDER_PASSWORD in masked or PLACEHOLDER_TOKEN in masked


# --------------------------------------------------------------------------
# personal data
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "curl -s 'https://t/api/user?mobile=13800138000'",
            f"curl -s 'https://t/api/user?mobile={PLACEHOLDER_PHONE}'",
        ),
        (
            "curl -s -d 'mobile=13800138000&code=1234' https://t/login",
            f"curl -s -d 'mobile={PLACEHOLDER_PHONE}&code=1234' https://t/login",
        ),
        (
            "curl -s 'https://t/sms?to=+8613800138000'",
            f"curl -s 'https://t/sms?to=+86{PLACEHOLDER_PHONE}'",
        ),
        ("GET /user/谭子清/permissions", f"GET /user/{PLACEHOLDER_NAME}/permissions"),
        ("GET /api/v1/users/谭子清/orders", f"GET /api/v1/users/{PLACEHOLDER_NAME}/orders"),
        (
            "curl -d 'name=谭子清&address=北京市朝阳区建国路1号' https://t/u",
            f"curl -d 'name={PLACEHOLDER_NAME}&address={PLACEHOLDER_ADDRESS}' https://t/u",
        ),
        (
            "curl -d 'realname=欧阳娜娜' https://t/u",
            f"curl -d 'realname={PLACEHOLDER_NAME}' https://t/u",
        ),
        (
            "curl -d 'mail=alice@corp.example' https://t/u",
            f"curl -d 'mail={PLACEHOLDER_EMAIL}' https://t/u",
        ),
        (
            f"echo {VALID_ID_CARD} > id.txt",
            f"echo {PLACEHOLDER_ID_CARD} > id.txt",
        ),
        (
            f"curl -d 'cardNo={VALID_BANK_CARD}' https://t/pay",
            f"curl -d 'cardNo={PLACEHOLDER_BANK_CARD}' https://t/pay",
        ),
    ],
)
def test_personal_data_is_masked(command: str, expected: str) -> None:
    masked, count = redact(command)
    assert masked == expected
    assert count >= 1


def test_id_card_needs_a_valid_checksum() -> None:
    # The whole reason this is checksum-gated: AXIOM payloads are full of
    # 18-digit snowflake ids, and masking one hides which object a command acts
    # on - the most useful single fact the judge gets.
    assert _is_id_card(VALID_ID_CARD)
    assert not _is_id_card("110101199003071234")
    assert not _is_id_card("971493486794096642")
    masked, count = redact('psql -c "select * from users where id=971493486794096642"')
    assert masked == 'psql -c "select * from users where id=971493486794096642"'
    assert count == 0


def test_bank_card_needs_key_and_luhn() -> None:
    assert _is_luhn(VALID_BANK_CARD)
    # A 16-19 digit run with no card-like key is an order number, not a card.
    assert redact("curl https://t/order/4111111111111111")[1] == 0
    # A card-like key whose value fails Luhn is left alone.
    assert redact("curl -d 'cardNo=4111111111111112' https://t/pay")[1] == 0


# --------------------------------------------------------------------------
# what must never be masked - these are the evidence the verdict rests on
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # -p is a port, a flag or a range far more often than it is a password
        "mkdir -p /tmp/scan",
        "nmap -p 80,443 target.example",
        "docker run -p 8080:80 nginx",
        "mysql -h 10.0.0.5 -P 3306 -u root orders",
        "psql -h 10.0.0.5 -p 5432 -U app orders",
        # curl -u without a colon is prompt mode, not a credential
        "curl -u admin https://t/x",
        # a key with no value
        'grep -r "password" /etc/app/config.yml',
        # infrastructure is not personal data
        "curl -X DELETE http://x/ap -d '{\"apMac\":\"02:00:00:00:09:01\"}'",
        "nmap -sV 10.0.0.0/24",
        "curl https://shk.kkguan.com/health",
        # route nouns are not names
        "GET /api/v1/用户管理/list",
        "GET /api/v2/会员/详情",
        # the shell's working directory is not a password
        "echo $PWD",
        "PWD=/tmp/x ls",
        # our own run directory
        "cd /var/lib/axiom/sandboxes/t_231/x && cat evidence/creds.json",
        # a plain non-secret name
        'curl -d "name=scan" https://t/u',
        # a word that merely contains a secret word is not a secret
        'curl -s "https://t/?bypass=1&compass=2"',
        # a user agent and a write-out format are not credentials
        'curl -A "Mozilla/5.0 (X11; Linux)" https://t/x',
        'curl -o /dev/null -w "%{http_code}" https://t/x',
    ],
)
def test_evidence_survives_untouched(command: str) -> None:
    assert redact(command) == (command, 0)


def test_a_command_injection_inside_a_header_is_not_hidden() -> None:
    # Masking the whole rest of the line would hide an attack from the judge.
    masked, _ = redact('curl -H "Authorization: Bearer abc; rm -rf /tmp/x" https://t')
    assert "rm -rf /tmp/x" in masked
    assert "abc" not in masked


# --------------------------------------------------------------------------
# invariants
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        f"curl -u admin:{SECRET} https://t/login",
        "curl -H 'Cookie: a=1; b=2' https://t/x",
        f"smbclient //h/s -U alice%{SECRET}",
        f"mysql -p{SECRET} -e 'select 1'",
        "GET /user/谭子清/permissions",
        f"echo {VALID_ID_CARD} > id.txt",
        "curl https://t/health",
    ],
)
def test_redaction_is_idempotent(command: str) -> None:
    once, _ = redact(command)
    twice, hits = redact(once)
    assert twice == once
    assert hits == 0


@pytest.mark.parametrize(
    "command",
    [
        f"curl -u admin:{SECRET} https://t/api/login",
        "curl -d '{\"password\":\"" + SECRET + "\"}' https://t/login",
        "GET /user/谭子清/permissions",
    ],
)
def test_structure_is_preserved(command: str) -> None:
    masked, _ = redact(command)
    # Same shell shape: the placeholder contains no quote, whitespace or
    # separator, so tokenising and splitting still give the same answer.
    assert len(masked.split()) == len(command.split())
    assert masked.count('"') == command.count('"')
    assert masked.count("'") == command.count("'")


def test_an_empty_command_is_not_an_error() -> None:
    assert redact("") == ("", 0)


# --------------------------------------------------------------------------
# wiring: what the judge is actually sent
# --------------------------------------------------------------------------


def test_judge_input_masks_before_the_text_leaves() -> None:
    request = GuardRequest(
        tool_name="bash",
        input={"command": f"curl -u admin:{SECRET} https://t/login"},
        phase="explore",
    )
    judged, count = _judge_input(normalize("bash", request.input), request)
    assert SECRET not in judged
    assert PLACEHOLDER_PASSWORD in judged
    assert judged.startswith("bash  curl -u admin:")
    assert judged.endswith("[phase] explore")
    assert count >= 1


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeClient:
    sent: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def post(self, url, headers=None, json=None):
        _FakeClient.sent.append({"url": url, "json": json or {}})
        return _FakeResponse("1:只读探测")


def _config() -> RuntimeConfig:
    return RuntimeConfig(
        guard_mode="enforce",
        guard_model_base_url="https://judge.example/v1",
        guard_model_name="judge",
        guard_api_key="test-key",
    )


def test_model_decision_sends_the_masked_text(monkeypatch) -> None:
    _FakeClient.sent = []
    monkeypatch.setattr("httpx.Client", _FakeClient)
    request = GuardRequest(
        tool_name="bash",
        input={"command": f"mysql -uroot -p{SECRET} -d orders"},
        phase="explore",
    )
    decision = _model_decision(request, normalize("bash", request.input), _config(), "prompt")

    assert decision.decision == "ALLOW"
    assert decision.source == "model"
    assert decision.redacted_count >= 1
    assert SECRET not in decision.judge_input
    assert len(_FakeClient.sent) == 1
    payload = _FakeClient.sent[0]["json"]
    sent = json.dumps(payload, ensure_ascii=False)
    assert SECRET not in sent
    assert PLACEHOLDER_PASSWORD in payload["messages"][1]["content"]


def test_a_failure_to_mask_never_falls_back_to_sending(monkeypatch) -> None:
    _FakeClient.sent = []
    monkeypatch.setattr("httpx.Client", _FakeClient)

    def explode(_text):
        raise RuntimeError("boom")

    monkeypatch.setattr("axiom.modules.guard.policy.redact", explode)
    request = GuardRequest(tool_name="bash", input={"command": f"curl -u a:{SECRET} https://t"}, phase="explore")
    decision = _model_decision(request, normalize("bash", request.input), _config(), "prompt")

    assert decision.source == "fail_open"
    assert decision.decision == "ALLOW"
    assert _FakeClient.sent == []


def test_masking_does_not_change_which_commands_are_judged() -> None:
    # Redaction runs last, so the allowlist and the hard rules still see the
    # command exactly as it will run.  If a placeholder ever leaked into that
    # path, the bracket would read as shell redirection and the verdicts below
    # would change.
    config = RuntimeConfig(guard_mode="enforce")
    allowlisted = [
        "cat /var/lib/axiom/contexts/t_231/graph.json",
        "ls -la",
        "grep password /etc/app/config.yml",
    ]
    for command in allowlisted:
        decision = evaluate(GuardRequest(tool_name="bash", input={"command": command}), config)
        assert decision.decision == "ALLOW", command

    # A hard rule fires on the raw text, before any masking.
    decision = evaluate(
        GuardRequest(tool_name="bash", input={"command": "curl -X DELETE https://t/api/orders/1"}),
        config,
        "red_team",
    )
    assert decision.decision == "BLOCK"
    assert decision.source == "hard_rule"


def test_the_audit_record_shows_what_was_actually_sent(client, monkeypatch) -> None:
    """The page must let the operator see both texts side by side.

    ``command`` stays raw because that is what an investigation is done on;
    ``judge_input`` is the masked form, so "nothing left in the clear" is a
    check the operator can make, not a claim they have to trust.
    """

    _FakeClient.sent = []

    project = client.post("/api/v1/projects", json={"title": "Redact"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "脱敏",
            "origin": "a.example.com",
            "guard_enabled": True,
        },
    ).json()
    current = client.get("/api/v1/settings/runtime").json()
    client.put(
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

    command = f"curl -s -X POST https://t/login -d '{{\"user\":\"admin\",\"password\":\"{SECRET}\"}}'"
    # Patch only around the guard call: the test client is itself an httpx
    # client, so a global patch would break the requests used to set the test up.
    monkeypatch.setattr("httpx.Client", _FakeClient)
    response = client.post(
        "/api/v1/internal/guard/pre",
        json={
            "project_id": task["project_id"],
            "task_id": task["id"],
            "phase": "explore",
            "worker": "worker-a",
            "tool_name": "bash",
            "input": {"command": command},
        },
    )
    monkeypatch.undo()
    assert response.status_code == 200, response.text

    # The judge never saw the password.
    assert len(_FakeClient.sent) == 1
    assert SECRET not in json.dumps(_FakeClient.sent[0]["json"], ensure_ascii=False)

    event = client.get(
        "/api/v1/audit-events",
        params={"task_id": task["id"], "event_type": "guard.decision"},
    ).json()[0]
    payload = event["payload"]
    assert payload["command"] == command
    assert SECRET not in payload["judge_input"]
    assert PLACEHOLDER_PASSWORD in payload["judge_input"]
    assert payload["redacted_count"] == 1


def test_a_keyed_value_is_masked_even_inside_a_sql_literal() -> None:
    """Documented limitation with a good side.

    ``password='Kkg@2023!'`` in a SQLi payload is masked, because the key/value
    rule cannot tell a SQL string literal from a shell argument - and masking it
    is the desirable outcome when the literal really is a credential.  The cost
    is that the judge sees ``password='[密码]'`` instead of the compared value.
    Every SQL keyword, the quoted shape and the payload structure survive, so
    the verdict still has what it needs; only the literal is gone.
    """

    command = (
        "curl -s \"https://t/p?id=1' AND (SELECT 1 FROM users WHERE password='Kkg@2023!')-- -\""
    )
    masked, count = redact(command)
    assert count == 1
    assert "Kkg@2023!" not in masked
    for keyword in ("SELECT", "FROM", "users", "WHERE", "password="):
        assert keyword in masked


# --------------------------------------------------------------------------
# shell variable assignments - the shape that actually leaked in production
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # Recorded verbatim from t_231.  The header reference below was masked
        # but the assignment above it kept the real session id in the clear.
        (
            'SID=3c80dc73-85b1-4e6c-9803-ff217acf64b5; curl -s -H "Cookie: sessionId=$SID" https://t/x',
            'SID=[令牌]; curl -s -H "Cookie: [Cookie]" https://t/x',
        ),
        (
            "TOK=de3de1276f4044df560297da726b0296; curl -H \"Account-Token: $TOK\" https://t/x",
            "TOK=[令牌]; curl -H \"Account-Token: [令牌]\" https://t/x",
        ),
        # same leak with the value in a default expansion
        (
            "SID=${SID:-3c80dc73-85b1-4e6c-9803-ff217acf64b5}; echo $SID",
            "SID=${SID:-[令牌]}; echo $SID",
        ),
        # camelCase holder of the same word
        (
            'cd /tmp && MY_TOKEN="abc123xyz" curl https://t',
            'cd /tmp && MY_TOKEN="[令牌]" curl https://t',
        ),
        ('MYSQL_PWD=x7Kd92Lq mysql -u root', f'MYSQL_PWD={PLACEHOLDER_PASSWORD} mysql -u root'),
        ('AK=AKIDabcdefghijklmn SK=Zm9vYmFyYmF6cXV4eA', f'AK={PLACEHOLDER_KEY} SK={PLACEHOLDER_KEY}'),
    ],
)
def test_shell_assignments_holding_a_secret_are_masked(command: str, expected: str) -> None:
    assert redact(command)[0] == expected


@pytest.mark.parametrize(
    "command",
    [
        # The variables in the very same scripts that must stay readable: the
        # judge needs the base URL and the ids to know what is being touched.
        "BASE=https://shk.kkguan.com/universe; U=https://t; OUT=/tmp/x",
        "P=3; PID=6028; T=$(cat /tmp/chen_token)",
        # a reference is not a value
        "SID=abc; echo $SID",
        "H=(-H 'Accept: json' -H 'X-Trace: 1')",
        # nothing secret in these names, even though they contain a secret word
        "monkey=banana; bypass=1; compassion=1",
        # PWD in shell is the working directory
        "PWD=/tmp/x ls",
        # a short value behind a two-letter name is a counter, not a key
        "AK=1 SK=2; for i in 1 2; do :; done",
    ],
)
def test_ordinary_shell_assignments_are_left_alone(command: str) -> None:
    assert redact(command) == (command, 0)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # The variable name is a single letter, so only the shape of the value
        # identifies it.  All three lines are verbatim from the recorded run.
        (
            "cd /tmp/i098 && S='3c80dc73-85b1-4e6c-9803-ff217acf64b5'; curl -s -H \"Cookie: $S\" https://t/x",
            "cd /tmp/i098 && S='[令牌]'; curl -s -H \"Cookie: [Cookie]\" https://t/x",
        ),
        (
            "cd /tmp && T=25fd91c492a4baad338abe12d62dc868; J=$(python3 -c 'x')",
            "cd /tmp && T=[令牌]; J=$(python3 -c 'x')",
        ),
        (
            "for s in 641eff2d-6f3f-4de1-b122-880229574988 5b62a3dc-099c-4df7-ab40-fc52e509409d; do echo $s; done",
            "for s in [令牌] [令牌]; do echo $s; done",
        ),
    ],
)
def test_a_credential_is_recognised_by_its_shape(command: str, expected: str) -> None:
    assert redact(command)[0] == expected


@pytest.mark.parametrize(
    "command",
    [
        # a UUID inside a target URL is infrastructure, not a credential: it
        # names the role being read, which is the evidence for the finding
        (
            "target='http://100.100.100.200/latest/meta-data/ram/security-credentials/"
            "KubernetesWorkerRole-ea60913a-d3da-4559-851e-b000cec53a38'"
        ),
        # short values and plain words never qualify
        "P=3; PID=6028; A=33311667307327",
        "for i in 1 2 3; do curl https://t/$i; done",
        "for f in *.json; do cat $f; done",
        "B='https://kpos-admin-sg.kkgroup.cn/shopping_api'; OUT=/tmp/x",
    ],
)
def test_shape_rule_does_not_reach_infrastructure(command: str) -> None:
    assert redact(command) == (command, 0)
