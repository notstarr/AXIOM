from __future__ import annotations

from sqlalchemy import Connection, delete, select, update

from axiom.modules.knowledge.schemas import CreateKnowledge, Knowledge, UpdateKnowledge
from axiom.modules.knowledge.tables import project_knowledge
from axiom.shared.ids import allocate_id


class KnowledgeRepository:
    def create(
        self,
        conn: Connection,
        *,
        project_id: str,
        command: CreateKnowledge,
        now: str,
    ) -> Knowledge:
        values = {
            "id": allocate_id(conn, scope=project_id, kind="knowledge", prefix="k"),
            "project_id": project_id,
            "title": command.title,
            "content": command.content,
            "category": command.category,
            "created_by": command.created_by,
            "created_at": now,
            "updated_at": now,
            "enabled": command.enabled,
            "archived": False,
        }
        conn.execute(project_knowledge.insert().values(**values))
        return Knowledge.model_validate(values)

    def get(self, conn: Connection, project_id: str, knowledge_id: str) -> Knowledge | None:
        row = conn.execute(
            select(project_knowledge).where(
                project_knowledge.c.project_id == project_id,
                project_knowledge.c.id == knowledge_id,
            )
        ).mappings().one_or_none()
        return Knowledge.model_validate(row) if row is not None else None

    def list(self, conn: Connection, project_id: str, *, include_archived: bool = False) -> list[Knowledge]:
        statement = select(project_knowledge).where(project_knowledge.c.project_id == project_id)
        if not include_archived:
            statement = statement.where(project_knowledge.c.archived.is_(False))
        rows = conn.execute(statement.order_by(project_knowledge.c.updated_at.desc())).mappings()
        return [Knowledge.model_validate(row) for row in rows]

    def update(
        self,
        conn: Connection,
        *,
        project_id: str,
        knowledge_id: str,
        command: UpdateKnowledge,
        now: str,
    ) -> Knowledge | None:
        values = command.model_dump(exclude_none=True)
        if values:
            values["updated_at"] = now
            result = conn.execute(
                update(project_knowledge)
                .where(
                    project_knowledge.c.project_id == project_id,
                    project_knowledge.c.id == knowledge_id,
                )
                .values(**values)
            )
            if not result.rowcount:
                return None
        return self.get(conn, project_id, knowledge_id)

    def delete(self, conn: Connection, project_id: str, knowledge_id: str) -> bool:
        return bool(
            conn.execute(
                delete(project_knowledge).where(
                    project_knowledge.c.project_id == project_id,
                    project_knowledge.c.id == knowledge_id,
                )
            ).rowcount
        )
