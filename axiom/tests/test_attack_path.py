"""Attack-path assembly.

A reporting Intent only ever sees its own slice: it holds a panel token and can reach an
internal host, but not that the token came from a metadata SSRF four Intents earlier.
These tests pin the graph walk that reconstructs that history for the report.
"""

from __future__ import annotations

from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from axiom.modules.graph.tables import facts, intent_sources, intents
from axiom.modules.redteam.tables import red_team_chains, red_team_results
from axiom.modules.reports.path import attack_path
from axiom.modules.projects.tables import projects
from axiom.modules.tasks.tables import tasks
from axiom.shared.clock import utcnow


def _task(conn, *, mode: str = "red_team") -> str:
    conn.execute(
        insert(projects).values(
            id="p001",
            title="红队项目",
            description="",
            status="active",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )
    conn.execute(
        insert(tasks).values(
            id="t001",
            project_id="p001",
            title="红队",
            origin_input="a.example.com",
            goal="取得目标能力",
            scope_json={},
            worker_pool_json=[],
            engagement_mode=mode,
            status="running",
            phase="explore",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )
    return "t001"


def _chain(conn, chain_id: str, title: str = "") -> None:
    conn.execute(
        insert(red_team_chains).values(
            id=chain_id,
            task_id="t001",
            title=title or chain_id,
            initial_principal_json={"id": "user:member"},
            target_capabilities_json=[],
            target_assets_json=[],
            status="open",
            status_reason="",
            current_state_json={},
            source_fact_ids_json=[],
            source_intent_ids_json=[],
            created_by="test",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )


def _fact(conn, fact_id: str, created_at: str = "2026-10-01T00:00:00Z") -> None:
    """Insert a fact once; later calls are no-ops so cycles can declare both ends."""
    conn.execute(
        pg_insert(facts)
        .values(
            id=fact_id,
            task_id="t001",
            description=f"fact {fact_id}",
            created_at=created_at,
        )
        .on_conflict_do_nothing()
    )


def _intent(
    conn,
    intent_id: str,
    *,
    chain_id: str | None,
    description: str,
    to_fact_id: str | None = None,
    created_at: str = "2026-10-01T00:00:00Z",
    consumes: tuple[str, ...] = (),
) -> None:
    # The produced fact must exist before the Intent that points at it.
    if to_fact_id:
        _fact(conn, to_fact_id, created_at)
    conn.execute(
        insert(intents).values(
            id=intent_id,
            task_id="t001",
            chain_id=chain_id,
            kind="explore",
            description=description,
            status="completed",
            creator="reason",
            to_fact_id=to_fact_id,
            created_at=created_at,
        )
    )
    for fact_id in consumes:
        conn.execute(
            insert(intent_sources).values(task_id="t001", intent_id=intent_id, fact_id=fact_id)
        )


def _result(conn, result_id: str, *, chain_id: str, intent_id: str, title: str, gain: str = "") -> None:
    conn.execute(
        insert(red_team_results).values(
            id=result_id,
            task_id="t001",
            chain_id=chain_id,
            intent_id=intent_id,
            result_type="permission",
            title=title,
            description="",
            principal="user:member",
            tenant="",
            capabilities_before_json=[],
            capabilities_gained_json=[],
            capabilities_after_json=[],
            target_asset="",
            data_fields_json=[],
            data_summary="",
            evidence_refs_json=[],
            reproduction_json={"chain_gain": gain} if gain else {},
            status="verified",
            reported_by="worker",
            points_declared=10,
            points_awarded=10,
            review_reason="",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )


def test_a_linear_chain_reads_oldest_first_and_marks_the_current_intent(engine) -> None:
    with engine.begin() as conn:
        _task(conn)
        _chain(conn, "ch001", "内网横向")
        _intent(conn, "i001", chain_id="ch001", description="目标：拿到第一层 token。判据：能调用接口", created_at="2026-10-01T00:00:00Z")
        _intent(conn, "i002", chain_id="ch001", description="目标：横向到内网主机。判据：能执行命令", created_at="2026-10-02T00:00:00Z")
        _result(conn, "x001", chain_id="ch001", intent_id="i001", title="取得面板 token", gain="持有 v004 面板 token")

    with engine.connect() as conn:
        links = attack_path(conn, task_id="t001", intent_id="i002", chain_id="ch001")

    assert [link.intent_id for link in links] == ["i001", "i002"]
    assert links[0].gain == "持有 v004 面板 token"
    assert links[1].is_current is True
    assert links[0].is_current is False
    # Each title is the brief's goal sentence, not the four-paragraph task text.
    assert links[0].title == "拿到第一层 token。"
    assert "判据" not in links[0].title


def test_a_cross_chain_path_is_prepended(engine) -> None:
    """The classic case: a metadata SSRF founded the chain that later moved laterally."""
    with engine.begin() as conn:
        _task(conn)
        _chain(conn, "ch001", "元数据 SSRF")
        _chain(conn, "ch002", "内网横向")
        _intent(
            conn,
            "i001",
            chain_id="ch001",
            description="目标：SSRF 打云元数据。判据：拿到 STS",
            to_fact_id="f001",
            created_at="2026-10-01T00:00:00Z",
        )
        _intent(
            conn,
            "i002",
            chain_id="ch002",
            description="目标：横向到 192.168.0.95。判据：能执行命令",
            created_at="2026-10-02T00:00:00Z",
            consumes=("f001",),
        )
        _result(conn, "x001", chain_id="ch001", intent_id="i001", title="取得 STS", gain="拿到 STS 凭证")

    with engine.connect() as conn:
        links = attack_path(conn, task_id="t001", intent_id="i002", chain_id="ch002")

    assert [link.intent_id for link in links] == ["i001", "i002"]
    assert [link.chain_id for link in links] == ["ch001", "ch002"]
    assert links[0].gain == "拿到 STS 凭证"
    assert links[1].is_current is True


def test_a_three_chain_prefix_keeps_chronological_order(engine) -> None:
    with engine.begin() as conn:
        _task(conn)
        for chain_id in ("ch001", "ch002", "ch003"):
            _chain(conn, chain_id)
        _intent(
            conn,
            "i001",
            chain_id="ch001",
            description="目标：第一步",
            to_fact_id="f001",
            created_at="2026-10-01T00:00:00Z",
        )
        _intent(
            conn,
            "i002",
            chain_id="ch002",
            description="目标：第二步",
            to_fact_id="f002",
            created_at="2026-10-02T00:00:00Z",
            consumes=("f001",),
        )
        _intent(
            conn,
            "i003",
            chain_id="ch003",
            description="目标：第三步",
            created_at="2026-10-03T00:00:00Z",
            consumes=("f002",),
        )

    with engine.connect() as conn:
        links = attack_path(conn, task_id="t001", intent_id="i003", chain_id="ch003")

    assert [link.intent_id for link in links] == ["i001", "i002", "i003"]


def test_a_cycle_between_chains_terminates(engine) -> None:
    with engine.begin() as conn:
        _task(conn)
        _chain(conn, "ch001")
        _chain(conn, "ch002")
        # Both facts are declared up front so each Intent can consume the other's:
        # a cycle the walk must survive rather than recurse forever.
        _fact(conn, "f001")
        _fact(conn, "f002")
        _intent(conn, "i001", chain_id="ch001", description="目标：A", to_fact_id="f001", created_at="2026-10-01T00:00:00Z", consumes=("f002",))
        _intent(conn, "i002", chain_id="ch002", description="目标：B", to_fact_id="f002", created_at="2026-10-02T00:00:00Z", consumes=("f001",))

    with engine.connect() as conn:
        links = attack_path(conn, task_id="t001", intent_id="i002", chain_id="ch002")

    assert [link.intent_id for link in links] == ["i001", "i002"]


def test_a_longer_path_than_the_cap_is_truncated_with_a_visible_marker(engine) -> None:
    with engine.begin() as conn:
        _task(conn)
        _chain(conn, "ch001")
        for index in range(1, 6):
            _intent(
                conn,
                f"i{index:03d}",
                chain_id="ch001",
                description=f"目标：第 {index} 环",
                created_at=f"2026-10-0{index}T00:00:00Z",
            )

    with engine.connect() as conn:
        links = attack_path(conn, task_id="t001", intent_id="i005", chain_id="ch001", max_links=3)

    assert len(links) == 3
    assert "已省略" in links[0].title
    # The truncation marker replaces the title of the earliest surviving link, and the
    # newest links (including the current one) are what remains.
    assert links[-1].intent_id == "i005"
    assert links[-1].is_current is True


def test_gain_falls_back_to_the_result_title_when_reproduction_has_none(engine) -> None:
    with engine.begin() as conn:
        _task(conn)
        _chain(conn, "ch001")
        _intent(conn, "i001", chain_id="ch001", description="目标：A", created_at="2026-10-01T00:00:00Z")
        _intent(conn, "i002", chain_id="ch001", description="目标：B", created_at="2026-10-02T00:00:00Z")
        _result(conn, "x001", chain_id="ch001", intent_id="i001", title="取得成员管理能力")

    with engine.connect() as conn:
        links = attack_path(conn, task_id="t001", intent_id="i002", chain_id="ch001")

    assert links[0].gain == "取得成员管理能力"


def test_an_unknown_intent_yields_an_empty_path_instead_of_raising(engine) -> None:
    with engine.begin() as conn:
        _task(conn)
        _chain(conn, "ch001")

    with engine.connect() as conn:
        assert attack_path(conn, task_id="t001", intent_id="i999", chain_id="ch001") == []


def test_a_task_with_no_intents_yields_an_empty_path(engine) -> None:
    with engine.begin() as conn:
        _task(conn)

    with engine.connect() as conn:
        assert attack_path(conn, task_id="t001", intent_id="i001", chain_id="ch001") == []


def test_the_path_carries_no_scores(engine) -> None:
    """Points belong to the review verdict; the narrative shown to a human must not
    leak the scoring record."""
    with engine.begin() as conn:
        _task(conn)
        _chain(conn, "ch001")
        _intent(conn, "i001", chain_id="ch001", description="目标：A", created_at="2026-10-01T00:00:00Z")
        _result(conn, "x001", chain_id="ch001", intent_id="i001", title="取得 token", gain="持有 token")

    with engine.connect() as conn:
        links = attack_path(conn, task_id="t001", intent_id="i001", chain_id="ch001")

    payload = links[0].as_dict()
    assert set(payload) == {"intent_id", "chain_id", "title", "gain", "is_current"}
    assert "points" not in repr(payload)
    assert "x001" not in repr(payload)


def test_the_path_stops_at_the_reported_intent(engine) -> None:
    """Later Intents must not appear in an earlier Result's report.

    The chain keeps growing; a report assembled at read time would otherwise gain links
    that did not contribute to the Result it describes.
    """
    with engine.begin() as conn:
        _task(conn)
        _chain(conn, "ch001")
        _intent(conn, "i001", chain_id="ch001", description="目标：第一步", created_at="2026-10-01T00:00:00Z")
        _intent(conn, "i002", chain_id="ch001", description="目标：第二步（本次成果）", created_at="2026-10-02T00:00:00Z")
        _intent(conn, "i003", chain_id="ch001", description="目标：第三步（之后才发生）", created_at="2026-10-03T00:00:00Z")

    with engine.connect() as conn:
        links = attack_path(conn, task_id="t001", intent_id="i002", chain_id="ch001")

    assert [link.intent_id for link in links] == ["i001", "i002"]
