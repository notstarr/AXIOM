from fastapi import APIRouter, HTTPException

from axiom.server.db import get_conn
from axiom.server.models import Fact
from axiom.server.services import check_project_active
from pydantic import BaseModel


class UpdateFactRequest(BaseModel):
    description: str


router = APIRouter(tags=["facts"])


@router.put(
    "/projects/{project_id}/facts/{fact_id}",
    response_model=Fact,
)
def update_fact(project_id: str, fact_id: str, body: UpdateFactRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = conn.execute(
            "SELECT * FROM facts WHERE id = ? AND project_id = ?",
            (fact_id, project_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Fact not found")

        conn.execute(
            "UPDATE facts SET description = ? WHERE id = ? AND project_id = ?",
            (body.description, fact_id, project_id),
        )

        updated = conn.execute(
            "SELECT * FROM facts WHERE id = ? AND project_id = ?",
            (fact_id, project_id),
        ).fetchone()
        return Fact(
            id=updated["id"],
            description=updated["description"],
        )


@router.delete(
    "/projects/{project_id}/facts/{fact_id}",
    status_code=204,
)
def delete_fact(project_id: str, fact_id: str):
    if fact_id in ("origin", "goal"):
        raise HTTPException(status_code=400, detail="Cannot delete origin or goal facts")
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = conn.execute(
            "SELECT * FROM facts WHERE id = ? AND project_id = ?",
            (fact_id, project_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Fact not found")

        # Remove intent_sources entries referencing this fact
        conn.execute(
            "DELETE FROM intent_sources WHERE project_id = ? AND fact_id = ?",
            (project_id, fact_id),
        )
        # Clear to_fact_id for intents that produced this fact
        conn.execute(
            "UPDATE intents SET to_fact_id = NULL WHERE project_id = ? AND to_fact_id = ?",
            (project_id, fact_id),
        )
        # Delete the fact
        conn.execute(
            "DELETE FROM facts WHERE id = ? AND project_id = ?",
            (fact_id, project_id),
        )


@router.put(
    "/projects/{project_id}/facts/{fact_id}/archive",
    response_model=Fact,
)
def toggle_fact_archive(project_id: str, fact_id: str):
    if fact_id in ("origin", "goal"):
        raise HTTPException(status_code=400, detail="Cannot archive origin or goal facts")
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = conn.execute(
            "SELECT * FROM facts WHERE id = ? AND project_id = ?",
            (fact_id, project_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Fact not found")
        new_val = 0 if row["archived"] else 1
        conn.execute(
            "UPDATE facts SET archived = ? WHERE id = ? AND project_id = ?",
            (new_val, fact_id, project_id),
        )
        updated = conn.execute(
            "SELECT * FROM facts WHERE id = ? AND project_id = ?",
            (fact_id, project_id),
        ).fetchone()
        return Fact(
            id=updated["id"],
            description=updated["description"],
            archived=bool(updated["archived"]),
        )
