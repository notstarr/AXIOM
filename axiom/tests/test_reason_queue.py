import os
from pathlib import Path
import pytest

from axiom.shared.database import create_database_engine, database_url_from_env
from axiom.composition import Container
from axiom.shared.clock import utcnow


def test_single_task_reason_queueing_and_late_binding():
    """测试单任务 Reason 排队与延迟全图绑定机制"""
    engine = create_database_engine(database_url_from_env())
    container = Container.build(
        engine,
        data_dir=Path(os.path.expanduser("~/.local/share/axiom-v3")),
        allow_startup_writes=False,
    )
    task_id = "t_172"
    now = utcnow()

    with engine.begin() as conn:
        in_flight = container.reason.repository.in_flight_for_task(conn, task_id)
        pending_runs = container.reason.repository.pending_runs_for_task(conn, task_id)
        print(f"[*] Task {task_id}: in_flight={in_flight.id if in_flight else None}, pending_count={len(pending_runs)}")

        # 验证 repository.refresh_run_sources 晚绑定机制
        latest_run = container.reason.repository.latest_completed_row(conn, task_id)
        if latest_run:
            run_id = latest_run["id"]
            container.reason.repository.refresh_run_sources(
                conn,
                task_id=task_id,
                run_id=run_id,
                source_fact_ids=["f001", "f002"],
                input_fingerprint="test_late_binding_fp",
            )
            sources = list(container.reason.repository.source_ids(conn, task_id, run_id))
            assert "f001" in sources
            assert "f002" in sources
            print("✓ Late-Binding 晚绑定 source_fact_ids 刷新成功")


def test_tasks_decoration_includes_queued_reasons():
    """测试 Task 接口装饰层正确透出 queued_reason_runs"""
    engine = create_database_engine(database_url_from_env())
    container = Container.build(
        engine,
        data_dir=Path(os.path.expanduser("~/.local/share/axiom-v3")),
        allow_startup_writes=False,
    )
    task = container.tasks.get("t_172")
    assert task is not None
    assert hasattr(task, "queued_reason_runs")
    print(f"✓ Task 模型成功挂载 queued_reason_runs 列表 (当前数量: {len(task.queued_reason_runs)})")
