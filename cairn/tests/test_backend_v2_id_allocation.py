from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import id_counters, metadata
from axiom.modules.common.ids import allocate_readable_id


def test_readable_ids_are_unique_when_workers_register_concurrently(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)

    def allocate(_index: int) -> str:
        with engine.begin() as conn:
            return allocate_readable_id(conn, scope="t_001", kind="artifact", prefix="a")

    with ThreadPoolExecutor(max_workers=8) as pool:
        allocated = list(pool.map(allocate, range(24)))

    assert sorted(allocated) == [f"a{index:03d}" for index in range(1, 25)]
    with engine.connect() as conn:
        assert conn.execute(
            select(id_counters.c.next_value).where(
                id_counters.c.scope == "t_001",
                id_counters.c.kind == "artifact",
            )
        ).scalar_one() == 25
