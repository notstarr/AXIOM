"""Scope red-team Result IDs to their Task (``x001`` instead of ``rr000000``).

PostgreSQL branch.  Result identity moves from ``(id)`` to ``(task_id, id)``
so each Task numbers its own Results from 1, exactly like ``v001`` for
Vulnerabilities.  Existing rows are renumbered in creation order.
"""

from alembic import op
import sqlalchemy as sa

from axiom.shared.database import AXIOM_SCHEMA


revision = "20261021_pg0026"
down_revision = "20261016_pg0025"
branch_labels = None
depends_on = None


LEGACY_ID_PATTERN = "^rr[0-9]+$"


def _columns(bind) -> list[str]:
    inspector = sa.inspect(bind)
    return [column["name"] for column in inspector.get_columns("red_team_results", schema=AXIOM_SCHEMA)]


def _copy_with_new_ids(bind, schema: str, id_expression: str) -> None:
    """Insert re-keyed copies so composite FKs can be re-pointed afterwards."""

    columns = _columns(bind)
    column_list = ", ".join(f'"{name}"' for name in columns)
    select_list = ", ".join(id_expression if name == "id" else f'r."{name}"' for name in columns)
    bind.execute(sa.text(
        f"""
        INSERT INTO {schema}.red_team_results ({column_list})
        SELECT {select_list}
          FROM {schema}.red_team_results r
          JOIN _rt_id_map m
            ON m.task_id = r.task_id AND m.old_id = r.id
        """
    ))


def _repoint_and_drop(bind, schema: str) -> None:
    bind.execute(sa.text(
        f"""
        UPDATE {schema}.vulnerability_review_runs v
           SET red_team_result_id = m.new_id
          FROM _rt_id_map m
         WHERE v.task_id = m.task_id
           AND v.red_team_result_id = m.old_id
        """
    ))
    bind.execute(sa.text(
        f"""
        DELETE FROM {schema}.red_team_results r
         USING _rt_id_map m
         WHERE r.task_id = m.task_id
           AND r.id = m.old_id
        """
    ))


def _result_fk_name(inspector) -> str | None:
    """The composite FK from vulnerability_review_runs onto red_team_results."""

    for fk in inspector.get_foreign_keys("vulnerability_review_runs", schema=AXIOM_SCHEMA):
        if fk.get("referred_table") == "red_team_results":
            return fk.get("name")
    return None


def upgrade() -> None:
    bind = op.get_bind()
    schema = AXIOM_SCHEMA
    inspector = sa.inspect(bind)
    pk = inspector.get_pk_constraint("red_team_results", schema=schema)
    fk_name = _result_fk_name(inspector)
    if pk.get("constrained_columns") == ["id"]:
        # The reviewer FK points at uq_red_team_results_task_id, so it has to
        # step aside while the unique key is folded into the new primary key.
        if fk_name:
            op.drop_constraint(fk_name, "vulnerability_review_runs", type_="foreignkey", schema=schema)
        op.drop_constraint("pk_red_team_results", "red_team_results", type_="primary", schema=schema)
        op.create_primary_key("pk_red_team_results", "red_team_results", ["task_id", "id"], schema=schema)
        unique_names = {
            constraint["name"]
            for constraint in sa.inspect(bind).get_unique_constraints("red_team_results", schema=schema)
        }
        if "uq_red_team_results_task_id" in unique_names:
            op.drop_constraint("uq_red_team_results_task_id", "red_team_results", type_="unique", schema=schema)
        if fk_name:
            op.create_foreign_key(
                fk_name,
                "vulnerability_review_runs",
                "red_team_results",
                ["task_id", "red_team_result_id"],
                ["task_id", "id"],
                source_schema=schema,
                referent_schema=schema,
                ondelete="CASCADE",
            )

    bind.execute(sa.text(
        f"""
        CREATE TEMP TABLE _rt_id_map ON COMMIT DROP AS
        SELECT task_id,
               id AS old_id,
               'x' || lpad(
                   (row_number() OVER (PARTITION BY task_id ORDER BY created_at, id))::text, 3, '0'
               ) AS new_id
          FROM {schema}.red_team_results
         WHERE id ~ :pattern
        """
    ), {"pattern": LEGACY_ID_PATTERN})
    _copy_with_new_ids(bind, schema, "m.new_id")
    _repoint_and_drop(bind, schema)

    # Counters: one per Task; the old global counter is retired.
    bind.execute(sa.text(f"DELETE FROM {schema}.id_counters WHERE scope = 'global' AND kind = 'red_team_result'"))
    bind.execute(sa.text(
        f"""
        INSERT INTO {schema}.id_counters (scope, kind, next_value)
        SELECT task_id, 'red_team_result', count(*) + 1
          FROM {schema}.red_team_results
         GROUP BY task_id
        ON CONFLICT (scope, kind)
        DO UPDATE SET next_value = GREATEST({schema}.id_counters.next_value, EXCLUDED.next_value)
        """
    ))


def downgrade() -> None:
    """Restore the global ``rr`` key shape with fresh, unique numbers."""

    bind = op.get_bind()
    schema = AXIOM_SCHEMA
    bind.execute(sa.text(
        f"""
        CREATE TEMP TABLE _rt_id_map ON COMMIT DROP AS
        SELECT task_id,
               id AS old_id,
               'rr' || lpad(
                   (row_number() OVER (PARTITION BY task_id ORDER BY created_at, id))::text, 6, '0'
               ) AS new_id
          FROM {schema}.red_team_results
         WHERE id !~ :pattern
        """
    ), {"pattern": LEGACY_ID_PATTERN})
    _copy_with_new_ids(bind, schema,
                       "'rr' || lpad(row_number() OVER (ORDER BY r.task_id, r.created_at, r.id)::text, 6, '0')")
    _repoint_and_drop(bind, schema)
    inspector = sa.inspect(bind)
    fk_name = _result_fk_name(inspector)
    if fk_name:
        op.drop_constraint(fk_name, "vulnerability_review_runs", type_="foreignkey", schema=schema)
    op.drop_constraint("pk_red_team_results", "red_team_results", type_="primary", schema=schema)
    op.create_primary_key("pk_red_team_results", "red_team_results", ["id"], schema=schema)
    op.create_unique_constraint(
        "uq_red_team_results_task_id", "red_team_results", ["task_id", "id"], schema=schema
    )
    if fk_name:
        op.create_foreign_key(
            fk_name,
            "vulnerability_review_runs",
            "red_team_results",
            ["task_id", "red_team_result_id"],
            ["task_id", "id"],
            source_schema=schema,
            referent_schema=schema,
            ondelete="CASCADE",
        )
