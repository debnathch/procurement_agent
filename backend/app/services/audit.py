import json
from sqlalchemy.orm import Session
from backend.app.models.entities import AuditEvent
def audit(db:Session,event_type:str,actor:str='system',entity_type:str|None=None,entity_id:str|None=None,details:dict|None=None):
    db.add(AuditEvent(event_type=event_type,actor=actor,entity_type=entity_type,entity_id=entity_id,details_json=json.dumps(details or {},default=str)))
