"""Scope red-team Chain IDs to their Task (``ch001`` instead of ``ch000113``).

PostgreSQL branch.  Chain identity moves from ``(id)`` to ``(task_id, id)`` so
each Task numbers its own Chains from 1, exactly like ``x001`` for Results and
``i001`` for Intents.  Existing rows are renumbered in creation order.

The single referencing foreign key is ``red_team_results(task_id, chain_id)``;
``intents.chain_id`` carries no constraint and is repointed explicitly.

Narrative references inside ``status_reason`` and ``audit_events`` are left
alone on purpose: they are the record of what was written at the time, not
live pointers.
"""

from alembic import op
import sqlalchemy as sa

from axiom.shared.database import AXIOM_SCHEMA


revision = "20261026_pg0027"
down_revision = "20261021_pg0026"
branch_labels = None
depends_on = None


LEGACY_ID_PATTERN = "^ch[0-9]+$"
NEW_ID_PATTERN = "^ch[0-9]{3}$"


def _chain_fk_name(inspector) -> str | None:
    """The composite FK from red_team_results onto red_team_chains."""

    for fk in inspector.get_foreign_keys("red_team_results", schema=AXIOM_SCHEMA):
        if fk.get("referred_table") == "red_team_chains":
            return fk.get("name")
    return None


def _build_map(
    bind, schema: str, id_expression: str, *, match: str, exclude: str | None = None
) -> None:
    # ``match`` and ``exclude`` are both needed here: old and new ids share the
    # ``ch`` prefix, so a re-run must skip rows that already carry a 3-digit id
    # instead of re-mapping them onto themselves and colliding on insert.
    extra = " AND id !~ :exclude" if exclude else ""
    bind.execute(
        sa.text(
            f"""
            CREATE TEMP TABLE _ch_id_map ON COMMIT DROP AS
            SELECT task_id,
                   id AS old_id,
                   {id_expression} AS new_id
              FROM {schema}.red_team_chains
             WHERE id ~ :match{extra}
            """
        ),
        {"match": match, "exclude": exclude},
    )


def _copy_with_new_ids(bind, schema: str) -> None:
    """Insert re-keyed copies so the referencing FK can be re-pointed first."""

    columns = [c["name"] for c in sa.inspect(bind).get_columns("red_team_chains", schema=schema)]
    column_list = ", ".join(f'"{name}"' for name in columns)
    select_list = ", ".join("m.new_id" if name == "id" else f'c."{name}"' for name in columns)
    bind.execute(
        sa.text(
            f"""
            INSERT INTO {schema}.red_team_chains ({column_list})
            SELECT {select_list}
              FROM {schema}.red_team_chains c
              JOIN _ch_id_map m
                ON m.task_id = c.task_id AND m.old_id = c.id
            """
        )
    )


def _repoint_and_drop(bind, schema: str) -> None:
    bind.execute(
        sa.text(
            f"""
            UPDATE {schema}.red_team_results r
               SET chain_id = m.new_id
              FROM _ch_id_map m
             WHERE r.task_id = m.task_id
               AND r.chain_id = m.old_id
            """
        )
    )
    bind.execute(
        sa.text(
            f"""
            UPDATE {schema}.intents i
               SET chain_id = m.new_id
              FROM _ch_id_map m
             WHERE i.task_id = m.task_id
               AND i.chain_id = m.old_id
            """
        )
    )
    bind.execute(
        sa.text(
            f"""
            DELETE FROM {schema}.red_team_chains c
             USING _ch_id_map m
             WHERE c.task_id = m.task_id
               AND c.id = m.old_id
            """
        )
    )


def _rebuild_counter(bind, schema: str, kind: str) -> None:
    bind.execute(
        sa.text(f"DELETE FROM {schema}.id_counters WHERE scope = 'global' AND kind = :kind"),
        {"kind": kind},
    )
    bind.execute(
        sa.text(
            f"""
            INSERT INTO {schema}.id_counters (scope, kind, next_value)
            SELECT task_id, :kind, count(*) + 1
              FROM {schema}.red_team_chains
             GROUP BY task_id
            ON CONFLICT (scope, kind)
            DO UPDATE SET next_value = GREATEST(
                {schema}.id_counters.next_value, EXCLUDED.next_value)
            """
        ),
        {"kind": kind},
    )


def upgrade() -> None:
    bind = op.get_bind()
    schema = AXIOM_SCHEMA
    inspector = sa.inspect(bind)
    pk = inspector.get_pk_constraint("red_team_chains", schema=schema)
    fk_name = _chain_fk_name(inspector)

    if pk.get("constrained_columns") == ["id"]:
        # The result FK points at uq_red_team_chains_task_id, so it has to step
        # aside while that unique key is folded into the new primary key.
        if fk_name:
            op.drop_constraint(
                fk_name, "red_team_results", type_="foreignkey", schema=schema
            )
        op.drop_constraint(
            "pk_red_team_chains", "red_team_chains", type_="primary", schema=schema
        )
        op.create_primary_key(
            "pk_red_team_chains", "red_team_chains", ["task_id", "id"], schema=schema
        )
        unique_names = {
            c["name"]
            for c in sa.inspect(bind).get_unique_constraints(
                "red_team_chains", schema=schema
            )
        }
        if "uq_red_team_chains_task_id" in unique_names:
            op.drop_constraint(
                "uq_red_team_chains_task_id", "red_team_chains", type_="unique", schema=schema
            )
        if fk_name:
            op.create_foreign_key(
                fk_name,
                "red_team_results",
                "red_team_chains",
                ["task_id", "chain_id"],
                ["task_id", "id"],
                source_schema=schema,
                referent_schema=schema,
                ondelete="CASCADE",
            )

    _build_map(
        bind,
        schema,
        "'ch' || lpad((row_number() OVER ("
        "PARTITION BY task_id ORDER BY created_at, id))::text, 3, '0')",
        match=LEGACY_ID_PATTERN,
        exclude=NEW_ID_PATTERN,
    )
    _copy_with_new_ids(bind, schema)
    _repoint_and_drop(bind, schema)
    _rebuild_counter(bind, schema, "red_team_chain")


def downgrade() -> None:
    """Restore the global six-digit ``ch`` key shape with fresh unique numbers."""

    bind = op.get_bind()
    schema = AXIOM_SCHEMA

    _build_map(
        bind,
        schema,
        "'ch' || lpad((row_number() OVER (ORDER BY task_id, created_at, id))::text, 6, '0')",
        match=NEW_ID_PATTERN,
    )
    _copy_with_new_ids(bind, schema)
    _repoint_and_drop(bind, schema)

    inspector = sa.inspect(bind)
    fk_name = _chain_fk_name(inspector)
    if fk_name:
        op.drop_constraint(fk_name, "red_team_results", type_="foreignkey", schema=schema)
    op.drop_constraint("pk_red_team_chains", "red_team_chains", type_="primary", schema=schema)
    op.create_primary_key("pk_red_team_chains", "red_team_chains", ["id"], schema=schema)
    op.create_unique_constraint(
        "uq_red_team_chains_task_id", "red_team_chains", ["task_id", "id"], schema=schema
    )
    if fk_name:
        op.create_foreign_key(
            fk_name,
            "red_team_results",
            "red_team_chains",
            ["task_id", "chain_id"],
            ["task_id", "id"],
            source_schema=schema,
            referent_schema=schema,
            ondelete="CASCADE",
        )

    op.execute(
        sa.text(
            f"""
            INSERT INTO {schema}.id_counters (scope, kind, next_value)
            SELECT 'global', 'red_team_chain', count(*) + 1
              FROM {schema}.red_team_chains
            ON CONFLICT (scope, kind)
            DO UPDATE SET next_value = GREATEST(
                {schema}.id_counters.next_value, EXCLUDED.next_value)
            """
        )
    )
