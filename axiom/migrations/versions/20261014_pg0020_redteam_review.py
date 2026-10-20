"""Allow the existing Reviewer scheduler to review RedTeamResult subjects."""
from alembic import op
import sqlalchemy as sa
from axiom.shared.database import AXIOM_SCHEMA

revision = "20261014_pg0020"
down_revision = "20261013_pg0019"
branch_labels = None
depends_on = None

def upgrade() -> None:
    bind = op.get_bind(); insp = sa.inspect(bind)
    if not insp.has_table("vulnerability_review_runs", schema=AXIOM_SCHEMA): return
    cols = {c["name"] for c in insp.get_columns("vulnerability_review_runs", schema=AXIOM_SCHEMA)}
    if "review_subject_type" not in cols:
        op.add_column("vulnerability_review_runs", sa.Column("review_subject_type", sa.String(), nullable=False, server_default="vulnerability"), schema=AXIOM_SCHEMA)
    if "red_team_result_id" not in cols:
        op.add_column("vulnerability_review_runs", sa.Column("red_team_result_id", sa.String(), nullable=True), schema=AXIOM_SCHEMA)
    op.alter_column("vulnerability_review_runs", "vulnerability_id", existing_type=sa.String(), nullable=True, schema=AXIOM_SCHEMA)

def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("vulnerability_review_runs", schema=AXIOM_SCHEMA):
        return
    # 指向 red_team_results 的外键由 pg0001 baseline 的 metadata.create_all() 依 ORM
    # 声明建立，名称是 SQLAlchemy 自动生成的，故按"引用目标"查找而不是硬编码约束名。
    # 不先摘掉它，pg0019 的 downgrade 就无法 DROP TABLE red_team_results
    # （DependentObjectsStillExist），整条 downgrade 到 base 的路径会断在这里。
    for fk in insp.get_foreign_keys("vulnerability_review_runs", schema=AXIOM_SCHEMA):
        if fk.get("referred_table") == "red_team_results" and fk.get("name"):
            op.drop_constraint(
                fk["name"], "vulnerability_review_runs", schema=AXIOM_SCHEMA, type_="foreignkey"
            )
    cols = {c["name"] for c in insp.get_columns("vulnerability_review_runs", schema=AXIOM_SCHEMA)}
    for name in ("red_team_result_id", "review_subject_type"):
        if name in cols:
            op.drop_column("vulnerability_review_runs", name, schema=AXIOM_SCHEMA)
    # vulnerability_id 的 nullable=True 不在此回滚：红队复核行该列为 NULL，
    # 强行恢复 NOT NULL 会让 downgrade 在有数据的库上直接失败。
