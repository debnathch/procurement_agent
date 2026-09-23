from datetime import datetime
from sqlalchemy.orm import Session
from backend.app.models.entities import ProcurementProposal,FeedbackEvent,Product,Supplier
from backend.app.adapters.executor import build_executor
from backend.app.services.guardrails import ProcurementGuardrails
from backend.app.services.audit import audit
from backend.app.core.config import settings
class ProposalService:
    def __init__(self,db:Session): self.db=db; self.guardrails=ProcurementGuardrails()
    def decide(self,proposal_id:int,action:str,approved_qty:float|None,supplier_id:str|None,reason:str|None,actor:str='human-ui'):
        proposal=self.db.get(ProcurementProposal,proposal_id)
        if not proposal: raise ValueError('Proposal not found.')
        product=self.db.get(Product,proposal.product_code)
        if not product: raise ValueError('Product for proposal no longer exists.')
        if action=='reject':
            if proposal.status!='PENDING': raise ValueError(f'Cannot reject proposal in state {proposal.status}.')
            proposal.status='REJECTED'; proposal.human_reason=reason or 'Rejected by human reviewer.'
            self.db.add(FeedbackEvent(proposal_id=proposal.id,action='REJECT',original_qty=proposal.recommended_qty,final_qty=None,reason=proposal.human_reason))
            audit(self.db,'PROPOSAL_REJECTED',actor=actor,entity_type='proposal',entity_id=str(proposal.id),details={'reason':proposal.human_reason})
            self.db.commit(); return {'status':proposal.status,'reference':''}
        if action!='approve': raise ValueError('Unsupported decision action.')
        qty=float(approved_qty if approved_qty is not None else proposal.recommended_qty); sid=supplier_id or proposal.supplier_id
        supplier=self.db.get(Supplier,sid) if sid else None
        if supplier is None: raise ValueError('An active supplier is required for approval.')
        if not settings.allow_supplier_change and sid!=proposal.supplier_id: raise ValueError('Changing supplier is disabled by policy.')
        guard=self.guardrails.validate_approval(proposal,product,supplier,qty)
        if not guard.allowed: raise ValueError('Guardrail blocked approval: '+' | '.join(guard.reasons))
        proposal.approved_qty=qty; proposal.supplier_id=supplier.supplier_id; proposal.supplier_name=supplier.supplier_name
        proposal.human_reason=reason or 'Approved by human reviewer.'; proposal.approved_at=datetime.utcnow(); proposal.status='APPROVED_PENDING_EXECUTION'
        self.db.add(FeedbackEvent(proposal_id=proposal.id,action='APPROVE' if qty==proposal.recommended_qty else 'MODIFY_APPROVE',original_qty=proposal.recommended_qty,final_qty=qty,reason=proposal.human_reason))
        audit(self.db,'PROPOSAL_APPROVED',actor=actor,entity_type='proposal',entity_id=str(proposal.id),details={'qty':qty,'supplier_id':supplier.supplier_id,'reason':proposal.human_reason})
        self.db.commit()
        result=build_executor().create_purchase_order(supplier_id=supplier.supplier_id,company_code=settings.marg_company_code,items=[{'product_code':proposal.product_code,'quantity':qty,'unit_cost':proposal.unit_cost}],remark=proposal.human_reason,idempotency_key=proposal.idempotency_key)
        proposal.status='EXECUTED' if result.success else 'APPROVED_PENDING_EXECUTION'
        if result.success: proposal.execution_reference=result.reference; proposal.executed_at=datetime.utcnow()
        audit(self.db,'PURCHASE_ORDER_EXECUTED' if result.success else 'PURCHASE_ORDER_EXECUTION_FAILED',actor='system',entity_type='proposal',entity_id=str(proposal.id),details={'reference':result.reference,'message':result.message})
        self.db.commit(); return {'status':proposal.status,'reference':result.reference,'message':result.message}
