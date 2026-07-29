"""Add reversible lineage archive batches."""

from __future__ import annotations

import hashlib

from alembic import op
import sqlalchemy as sa


revision = "20260729_0025"
down_revision = "20260728_0024"
branch_labels = None
depends_on = None


ENTITY_TABLES = (
    ("fact", "facts"),
    ("intent", "intents"),
    ("artifact", "artifacts"),
    ("vulnerability", "vulnerabilities"),
    ("todo", "todos"),
)


def upgrade() -> None:
    op.create_table(
        "archive_batches",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("root_type", sa.String(), nullable=False),
        sa.Column("root_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("closure_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("restored_at", sa.String(), nullable=True),
        sa.Column("restored_by", sa.String(), nullable=True),
        sa.CheckConstraint(
            "root_type IN ('fact','intent','artifact','vulnerability','todo')",
            name="ck_archive_batches_root_type",
        ),
        sa.CheckConstraint(
            "status IN ('active','restored')",
            name="ck_archive_batches_status",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_archive_batches_task_status",
        "archive_batches",
        ["task_id", "status"],
    )
    op.create_index(
        "ix_archive_batches_task_root",
        "archive_batches",
        ["task_id", "root_type", "root_id"],
    )
    op.create_table(
        "archive_batch_members",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("archive_id", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("relation", sa.String(), nullable=False),
        sa.CheckConstraint(
            "entity_type IN ('fact','intent','artifact','vulnerability','todo')",
            name="ck_archive_batch_members_entity_type",
        ),
        sa.CheckConstraint(
            "relation IN ('root','producer','output','descendant','related')",
            name="ck_archive_batch_members_relation",
        ),
        sa.ForeignKeyConstraint(
            ["archive_id"],
            ["archive_batches.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("archive_id", "entity_type", "entity_id"),
    )
    op.create_index(
        "ix_archive_batch_members_task_entity",
        "archive_batch_members",
        ["task_id", "entity_type", "entity_id"],
    )

    bind = op.get_bind()
    created_at = "1970-01-01T00:00:00+00:00"
    for entity_type, table_name in ENTITY_TABLES:
        rows = bind.execute(
            sa.text(f"SELECT task_id, id FROM {table_name} WHERE archived = 1")
        ).mappings()
        for row in rows:
            archive_id = f"legacy:{entity_type}:{row['task_id']}:{row['id']}"
            closure_hash = hashlib.sha256(archive_id.encode()).hexdigest()
            bind.execute(
                sa.text(
                    "INSERT INTO archive_batches "
                    "(id, task_id, root_type, root_id, status, reason, actor, "
                    "closure_hash, created_at, restored_at, restored_by) "
                    "VALUES (:id, :task_id, :root_type, :root_id, 'active', "
                    "'迁移前已有归档记录', 'migration.archive_batches', "
                    ":closure_hash, :created_at, NULL, NULL)"
                ),
                {
                    "id": archive_id,
                    "task_id": row["task_id"],
                    "root_type": entity_type,
                    "root_id": row["id"],
                    "closure_hash": closure_hash,
                    "created_at": created_at,
                },
            )
            bind.execute(
                sa.text(
                    "INSERT INTO archive_batch_members "
                    "(task_id, archive_id, entity_type, entity_id, relation) "
                    "VALUES (:task_id, :archive_id, :entity_type, :entity_id, 'root')"
                ),
                {
                    "task_id": row["task_id"],
                    "archive_id": archive_id,
                    "entity_type": entity_type,
                    "entity_id": row["id"],
                },
            )


def downgrade() -> None:
    op.drop_index(
        "ix_archive_batch_members_task_entity",
        table_name="archive_batch_members",
    )
    op.drop_table("archive_batch_members")
    op.drop_index("ix_archive_batches_task_root", table_name="archive_batches")
    op.drop_index("ix_archive_batches_task_status", table_name="archive_batches")
    op.drop_table("archive_batches")
