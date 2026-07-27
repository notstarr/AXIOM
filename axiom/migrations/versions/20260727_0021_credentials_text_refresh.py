"""Store credentials and refresh instructions as progressively disclosed text."""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "20260727_0021"
down_revision = "20260727_0020"
branch_labels = None
depends_on = None


_LEGACY_MARKER = "[旧版迁移信息]"


def upgrade() -> None:
    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.text(
                "SELECT id, credential_type, target_host, data_json, "
                "created_at, updated_at "
                "FROM credentials ORDER BY id"
            )
        ).mappings()
    )

    with op.batch_alter_table("credentials") as batch:
        batch.add_column(sa.Column("content", sa.Text(), nullable=True))
        batch.add_column(sa.Column("notes", sa.Text(), nullable=True))

    refreshes: list[tuple[str, str, str, str]] = []
    for row in rows:
        content, notes, refresh_content = _upgrade_material(row)
        connection.execute(
            sa.text(
                "UPDATE credentials SET content = :content, notes = :notes "
                "WHERE id = :credential_id"
            ),
            {
                "credential_id": row["id"],
                "content": content,
                "notes": notes,
            },
        )
        if refresh_content is not None:
            refreshes.append(
                (
                    str(row["id"]),
                    refresh_content,
                    str(row["created_at"]),
                    str(row["updated_at"]),
                )
            )

    with op.batch_alter_table("credentials") as batch:
        batch.drop_column("credential_type")
        batch.drop_column("target_host")
        batch.drop_column("data_json")
        batch.alter_column("content", existing_type=sa.Text(), nullable=False)

    op.create_table(
        "credential_refreshes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("credential_id", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["credentials.id"],
            name="fk_credential_refreshes_credential_id_credentials",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_credential_refreshes"),
        sa.UniqueConstraint(
            "credential_id",
            name="uq_credential_refreshes_credential_id",
        ),
    )
    _restore_refreshes(connection, refreshes)


def downgrade() -> None:
    connection = op.get_bind()
    refresh_by_credential = {
        row["credential_id"]: row["content"]
        for row in connection.execute(
            sa.text("SELECT credential_id, content FROM credential_refreshes")
        ).mappings()
    }
    op.drop_table("credential_refreshes")

    rows = list(
        connection.execute(
            sa.text("SELECT id, content, notes FROM credentials ORDER BY id")
        ).mappings()
    )
    with op.batch_alter_table("credentials") as batch:
        batch.add_column(sa.Column("credential_type", sa.String(), nullable=True))
        batch.add_column(sa.Column("target_host", sa.String(), nullable=True))
        batch.add_column(sa.Column("data_json", sa.Text(), nullable=True))

    for row in rows:
        credential_type, domains, notes = _downgrade_metadata(row["notes"])
        data = _downgrade_material(row["content"])
        if notes:
            data["_legacy_notes"] = notes
        data["_legacy_domains"] = domains
        refresh_content = refresh_by_credential.get(row["id"])
        if refresh_content:
            data["_legacy_refresh"] = refresh_content
        connection.execute(
            sa.text(
                "UPDATE credentials SET credential_type = :credential_type, "
                "target_host = :target_host, data_json = :data_json "
                "WHERE id = :credential_id"
            ),
            {
                "credential_id": row["id"],
                "credential_type": credential_type,
                "target_host": domains[0] if domains else "",
                "data_json": json.dumps(data, ensure_ascii=False),
            },
        )

    with op.batch_alter_table("credentials") as batch:
        batch.drop_column("content")
        batch.drop_column("notes")
        batch.alter_column(
            "credential_type",
            existing_type=sa.String(),
            nullable=False,
        )
        batch.alter_column(
            "target_host",
            existing_type=sa.String(),
            nullable=False,
        )
        batch.alter_column("data_json", existing_type=sa.Text(), nullable=False)


def _upgrade_material(row) -> tuple[str, str | None, str | None]:
    raw = str(row["data_json"] or "")
    legacy_notes: str | None = None
    legacy_domains: list[str] = []
    legacy_refresh: str | None = None
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        content = raw or "{}"
    else:
        if isinstance(parsed, dict):
            legacy_notes = _optional_text(parsed.pop("_legacy_notes", None))
            legacy_domains = _string_list(parsed.pop("_legacy_domains", None))
            legacy_refresh = _refresh_text(parsed.pop("_legacy_refresh", None))
        content = json.dumps(parsed, ensure_ascii=False, indent=2)

    target_host = str(row["target_host"] or "").strip()
    domains = list(
        dict.fromkeys([*([target_host] if target_host else []), *legacy_domains])
    )
    metadata = _legacy_metadata(str(row["credential_type"] or "custom"), domains)
    notes = "\n\n".join(part for part in (legacy_notes, metadata) if part)
    return content, notes or None, legacy_refresh


def _restore_refreshes(connection, refreshes: list[tuple[str, str, str, str]]) -> None:
    if not refreshes:
        return
    next_value = connection.execute(
        sa.text(
            "SELECT next_value FROM id_counters "
            "WHERE scope = 'global' AND kind = 'credential_refresh'"
        )
    ).scalar_one_or_none()
    sequence = max(int(next_value or 1), 1)
    for credential_id, content, created_at, updated_at in refreshes:
        connection.execute(
            sa.text(
                "INSERT INTO credential_refreshes "
                "(id, credential_id, content, created_at, updated_at) "
                "VALUES (:id, :credential_id, :content, :created_at, :updated_at)"
            ),
            {
                "id": f"crf_{sequence:03d}",
                "credential_id": credential_id,
                "content": content,
                "created_at": created_at,
                "updated_at": updated_at,
            },
        )
        sequence += 1
    updated = connection.execute(
        sa.text(
            "UPDATE id_counters SET next_value = :next_value "
            "WHERE scope = 'global' AND kind = 'credential_refresh'"
        ),
        {"next_value": sequence},
    )
    if not updated.rowcount:
        connection.execute(
            sa.text(
                "INSERT INTO id_counters (scope, kind, next_value) "
                "VALUES ('global', 'credential_refresh', :next_value)"
            ),
            {"next_value": sequence},
        )


def _legacy_metadata(credential_type: str, domains: list[str]) -> str:
    domain_text = "、".join(domains) if domains else "（未设置）"
    return (
        f"{_LEGACY_MARKER}\n"
        f"认证类型：{credential_type.strip() or 'custom'}\n"
        f"适用域名：{domain_text}"
    )


def _downgrade_metadata(value: str | None) -> tuple[str, list[str], str | None]:
    notes = str(value or "").strip()
    marker = f"\n\n{_LEGACY_MARKER}\n"
    if notes.startswith(f"{_LEGACY_MARKER}\n"):
        human_notes = ""
        block = notes[len(_LEGACY_MARKER) + 1 :]
    elif marker in notes:
        human_notes, block = notes.rsplit(marker, 1)
    else:
        return "custom", [], notes or None

    fields: dict[str, str] = {}
    for line in block.splitlines():
        key, separator, item = line.partition("：")
        if separator:
            fields[key.strip()] = item.strip()
    domains_text = fields.get("适用域名", "")
    domains = (
        []
        if not domains_text or domains_text == "（未设置）"
        else [item.strip() for item in domains_text.split("、") if item.strip()]
    )
    return fields.get("认证类型", "custom") or "custom", domains, human_notes or None


def _downgrade_material(content: str) -> dict[str, object]:
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return {"content": content}
    return parsed if isinstance(parsed, dict) else {"content": content}


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _refresh_text(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("content")
    return _optional_text(value)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
