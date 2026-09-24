"""
Audit Logging Service

Records structured, immutable audit log events into the `audit_events` database table.
Used to track system operations, agent runs, human reviews, approvals, rejections,
and external execution results for pharmaceutical regulatory compliance.
"""

import json
from sqlalchemy.orm import Session
from backend.app.models.entities import AuditEvent


def audit(
    db: Session,
    event_type: str,
    actor: str = "system",
    entity_type: str | None = None,
    entity_id: str | None = None,
    details: dict | None = None,
) -> None:
    """
    Log an event to the immutable audit trail.

    Args:
        db (Session): SQLAlchemy database session.
        event_type (str): Categorical event name (e.g. 'PROPOSAL_APPROVED', 'MARG_EXCEL_INGESTED').
        actor (str): Identity of the caller (e.g. 'system', 'human-reviewer', 'user-upload').
        entity_type (str | None): Type of entity affected (e.g. 'proposal', 'run', 'product', 'file').
        entity_id (str | None): Identifier of the affected entity (e.g. proposal ID, run ID).
        details (dict | None): Contextual metadata serialized as JSON into details_json.
    """
    event = AuditEvent(
        event_type=event_type,
        actor=actor,
        entity_type=entity_type,
        entity_id=entity_id,
        details_json=json.dumps(details or {}, default=str),
    )
    db.add(event)
