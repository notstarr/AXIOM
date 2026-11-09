"""Worker 指令：作用域、一次性投递与提示词通道。

两种生命周期的边界全在这里守：

* 常驻指令每个 execution input 现查，按 phase 分流，不写 delivery 台账；
* 一次性指令在「领取 / 新建 run」时冻结成 delivery，``/input`` 只读台账，
  所以同一 lease 重试四次不会重投，lease 过期换 id 也不会漏投。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import insert, select

from axiom.modules.directives.schemas import DirectiveItem, WorkerDirectiveBundle
from axiom.modules.directives.tables import directive_deliveries
from axiom.modules.graph.tables import intents
from axiom.shared.clock import utcnow


def _task(client) -> dict:
    project = client.post("/api/v1/projects", json={"title": "指令测试"}).json()
    return client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Directive",
            "origin": "a.example.com",
        },
    ).json()


def _create(client, task_id: str, *, scope: str, content: str = "做 X", **extra):
    return client.post(
        f"/api/v1/tasks/{task_id}/directives",
        json={"content": content, "creator": "人工", "scope": scope, **extra},
    )


def _pending_explore(client, task_id: str, intent_id: str = "i002", status: str = "pending") -> None:
    engine = client.app.state.container.engine
    with engine.begin() as conn:
        conn.execute(
            insert(intents).values(
                id=intent_id,
                task_id=task_id,
                kind="explore",
                description="待领取的 Explore",
                status=status,
                creator="reason",
                created_at=utcnow(),
            )
        )


def test_all_five_scopes_create_with_the_expected_lifetime(client) -> None:
    task = _task(client)
    expected = {
        "reason_all": "persistent",
        "reason_next": "once",
        "explore_all": "persistent",
        "all_workers": "persistent",
    }
    for scope, lifetime in expected.items():
        response = _create(client, task["id"], scope=scope, content=f"指令 {scope}")
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["scope"] == scope
        assert body["lifetime"] == lifetime
        assert body["status"] == ("active" if lifetime == "persistent" else "pending")
        assert body["target_intent_id"] is None

    _pending_explore(client, task["id"])
    one = _create(
        client,
        task["id"],
        scope="explore_one",
        content="指令 explore_one",
        target_intent_id="i002",
    )
    assert one.status_code == 201, one.text
    assert one.json()["lifetime"] == "once"
    assert one.json()["status"] == "pending"
    assert one.json()["target_intent_id"] == "i002"


def test_explore_one_only_targets_a_pending_explore(client) -> None:
    """不做运行中插话：已 claimed/running 的 Explore 一律拒绝下发。"""
    task = _task(client)
    missing = _create(client, task["id"], scope="explore_one", target_intent_id="i999")
    assert missing.status_code == 409, missing.text

    _pending_explore(client, task["id"], intent_id="i003", status="claimed")
    claimed = _create(client, task["id"], scope="explore_one", target_intent_id="i003")
    assert claimed.status_code == 409, claimed.text

    no_target = _create(client, task["id"], scope="reason_all", target_intent_id="i003")
    assert no_target.status_code == 422, no_target.text

    _pending_explore(client, task["id"], intent_id="i002")
    eligible = client.get(
        f"/api/v1/tasks/{task['id']}/directives/eligible-explores"
    ).json()
    assert [item["intent_id"] for item in eligible] == ["i002"]


def test_client_message_id_makes_creation_idempotent(client) -> None:
    task = _task(client)
    body = {
        "content": "只发一次",
        "creator": "人工",
        "scope": "reason_next",
        "client_message_id": "m-1",
    }
    url = f"/api/v1/tasks/{task['id']}/directives"
    first = client.post(url, json=body)
    second = client.post(url, json=body)
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get(url).json()) == 1


def test_persistent_directives_resolve_only_in_their_own_phase(client) -> None:
    """常驻三作用域：阶段章节只收自己的，系统前缀三个 phase 都看得到。"""
    task = _task(client)
    for scope in ("reason_all", "explore_all", "all_workers"):
        created = _create(client, task["id"], scope=scope, content=f"内容 {scope}")
        assert created.status_code == 201, created.text

    service = client.app.state.container.directives
    with client.app.state.container.engine.connect() as conn:
        reason = service.resolve(
            conn, task_id=task["id"], phase="reason", lease_id="L"
        )
        explore = service.resolve(
            conn, task_id=task["id"], phase="explore", lease_id="L"
        )
        plan = service.resolve(
            conn, task_id=task["id"], phase="plan", lease_id="L"
        )

    assert [item.content for item in reason.reason_all] == ["内容 reason_all"]
    assert not reason.explore_all
    assert [item.content for item in explore.explore_all] == ["内容 explore_all"]
    assert not explore.reason_all
    assert not plan.reason_all and not plan.explore_all
    for bundle in (reason, explore, plan):
        assert [item.content for item in bundle.all_workers] == ["内容 all_workers"]


def test_explore_one_delivery_is_frozen_per_lease_and_reopens_when_unused(client) -> None:
    """领取时冻结、/input 只读；没真正取走的指令在租约失败后重开为 pending。"""
    task = _task(client)
    _pending_explore(client, task["id"])
    created = _create(
        client,
        task["id"],
        scope="explore_one",
        content="本次专项",
        target_intent_id="i002",
    )
    directive_id = created.json()["id"]
    service = client.app.state.container.directives
    engine = client.app.state.container.engine

    with engine.begin() as conn:
        delivered = service.deliver_for_intent(
            conn, task_id=task["id"], intent_id="i002", lease_id="L1"
        )
    assert [row["id"] for row in delivered] == [directive_id]

    # 同一 lease 反复 /input：台账里只有一条，bundle 内容稳定。
    with engine.begin() as conn:
        first = service.resolve(
            conn,
            task_id=task["id"],
            phase="explore",
            lease_id="L1",
            intent_id="i002",
        )
        service.mark_delivered(conn, task_id=task["id"], lease_id="L1")
        second = service.resolve(
            conn,
            task_id=task["id"],
            phase="explore",
            lease_id="L1",
            intent_id="i002",
        )
    assert [item.id for item in first.explore_once] == [directive_id]
    assert [item.id for item in second.explore_once] == [directive_id]
    with engine.connect() as conn:
        rows = conn.execute(select(directive_deliveries)).mappings().all()
    assert len(rows) == 1

    # 已经投递成功的不重开：worker 后续崩溃不该让同一句话再发一遍。
    with engine.begin() as conn:
        assert service.reopen_queued(conn, task_id=task["id"], lease_id="L1") == []

    # 新指令领取后还没被 /input 取走就失败：重开，等下一次领取。
    second_directive = _create(
        client,
        task["id"],
        scope="explore_one",
        content="重开验证",
        target_intent_id="i002",
    ).json()
    with engine.begin() as conn:
        service.deliver_for_intent(
            conn, task_id=task["id"], intent_id="i002", lease_id="L2"
        )
        reopened = service.reopen_queued(
            conn, task_id=task["id"], lease_id="L2"
        )
    assert second_directive["id"] in reopened
    listed = client.get(f"/api/v1/tasks/{task['id']}/directives").json()
    by_id = {item["id"]: item for item in listed}
    assert by_id[second_directive["id"]]["status"] == "pending"
    assert by_id[directive_id]["status"] == "delivered"


def test_directive_prompt_blocks_are_scoped_to_their_channels() -> None:
    """三条通道各自只带自己的内容，空 bundle 不产生任何 block。"""
    bundle = WorkerDirectiveBundle(
        all_workers=[DirectiveItem(id="dir_1", content="全局规则")],
        reason_all=[DirectiveItem(id="dir_2", content="Reason 常驻")],
        reason_once=[DirectiveItem(id="dir_3", content="本轮一次性")],
        explore_all=[DirectiveItem(id="dir_4", content="Explore 常驻")],
        explore_once=[DirectiveItem(id="dir_5", content="本次一次性")],
    )
    system = bundle.render_system_block()
    assert "全局规则" in system
    assert "BEGIN AXIOM WORKER DIRECTIVES" in system
    assert "系统规则" in system  # 优先级声明，明确指令不能覆盖阶段合同
    assert "Reason 常驻" not in system and "Explore 常驻" not in system

    reason = bundle.render_reason_section()
    assert "Reason 常驻" in reason and "本轮一次性" in reason
    assert "Explore 常驻" not in reason and "全局规则" not in reason

    explore = bundle.render_explore_section()
    assert "Explore 常驻" in explore and "本次一次性" in explore
    assert "Reason 常驻" not in explore and "全局规则" not in explore

    empty = WorkerDirectiveBundle()
    assert empty.render_system_block() == ""
    assert empty.render_reason_section() == ""
    assert empty.render_explore_section() == ""


def test_all_reason_and_explore_templates_expose_the_directive_placeholder() -> None:
    """PromptRenderer 是纯 str.replace：漏写一个红队变体会静默收不到指令。"""
    root = Path(__file__).parents[1] / "src/axiom/dispatcher/prompts"
    for name in (
        "reason.md",
        "reason_redteam.md",
        "explore.md",
        "explore_redteam.md",
    ):
        text = (root / name).read_text(encoding="utf-8")
        assert "{directives}" in text, name
        assert "hints.json" not in text, name


def test_legacy_hint_writes_are_gone_but_reads_still_work(client) -> None:
    """停写不删表：历史行仍可读，导出与 Todo/Goal 的软引用不悬空。"""
    task = _task(client)
    assert (
        client.post(
            f"/api/v1/tasks/{task['id']}/hints", json={"content": "x"}
        ).status_code
        == 410
    )
    assert (
        client.patch(
            f"/api/v1/tasks/{task['id']}/hints/h001", json={"content": "x"}
        ).status_code
        == 410
    )
    assert client.get(f"/api/v1/tasks/{task['id']}/hints").json() == []


# ── 预设与「建任务时随附指令」────────────────────────────────────────────────

def test_presets_are_served_without_a_task(client) -> None:
    """预设要在建任务之前就能拿到——那时候还没有 task_id，端点不能挂在任务路径下。"""
    response = client.get("/api/v1/directive-presets")

    assert response.status_code == 200, response.text
    presets = response.json()
    scopes = {item["scope"] for item in presets}
    assert {"reason_all", "explore_all", "all_workers"} <= scopes
    # 三个用户要求的默认模板都在
    labels = {item["label"] for item in presets}
    assert "禁止测试指定资产" in labels
    assert "痕迹统一标记" in labels
    assert "不派发越权类测试" in labels
    for item in presets:
        assert item["template"].strip()


def test_task_creation_persists_the_three_directives(client) -> None:
    """建任务时随附的指令要和任务同一事务落库，建完就能在任务里看到。"""
    project = client.post("/api/v1/projects", json={"title": "带指令建任务"}).json()
    created = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "建时带指令",
            "origin": "a.example.com",
            "directives": [
                {"scope": "all_workers", "content": "不允许测试 pay.example.com"},
                {"scope": "explore_all", "content": "痕迹统一标记为 SecPentest-"},
                {"scope": "reason_all", "content": "不派发越权类测试"},
            ],
        },
    )

    assert created.status_code == 201, created.text
    task = created.json()
    directives = client.get(f"/api/v1/tasks/{task['id']}/directives").json()

    assert len(directives) == 3
    by_scope = {item["scope"]: item for item in directives}
    assert by_scope["all_workers"]["content"] == "不允许测试 pay.example.com"
    assert by_scope["explore_all"]["content"] == "痕迹统一标记为 SecPentest-"
    assert by_scope["reason_all"]["content"] == "不派发越权类测试"
    # 建任务时只允许常驻作用域，三条都该是 active + persistent
    for item in directives:
        assert item["status"] == "active"
        assert item["lifetime"] == "persistent"
        assert item["target_intent_id"] is None


def test_task_creation_without_directives_stays_empty(client) -> None:
    """不带指令是默认行为，不能凭空多出三条空指令。"""
    task = _task(client)
    assert client.get(f"/api/v1/tasks/{task['id']}/directives").json() == []


def test_task_creation_rejects_once_scopes(client) -> None:
    """「仅一次」要绑定具体的 run/intent，建任务时这两者都还不存在，必须拒。"""
    project = client.post("/api/v1/projects", json={"title": "非法作用域"}).json()
    response = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "非法作用域",
            "origin": "a.example.com",
            "directives": [{"scope": "reason_next", "content": "只对下一轮说"}],
        },
    )
    assert response.status_code == 422, response.text
