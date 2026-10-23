from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from qal.models import AuditEvent


def record(session: Session, actor: str, action: str, entity_type: str, entity_id: str | None, **details: Any) -> None:
    session.add(AuditEvent(actor=actor, action=action, entity_type=entity_type, entity_id=entity_id, details=details))
