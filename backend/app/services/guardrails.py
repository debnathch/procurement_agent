from dataclasses import dataclass
from backend.app.core.config import settings
from backend.app.models.entities import Product,Supplier,ProcurementProposal
@dataclass
class GuardrailResult:
    allowed:bool
    reasons:list[str]
    warnings:list[str]
class ProcurementGuardrails:
    def validate_proposal(self,product:Product,supplier:Supplier|None,qty:float,unit_cost:float)->GuardrailResult:
        reasons=[]; warnings=[]; value=max(0,qty)*max(0,unit_cost); pack=max(1,int(product.pack_size))
        if qty<=0: reasons.append('Quantity must be greater than zero.')
        if qty>settings.max_proposal_qty_units: reasons.append(f'Quantity exceeds hard cap of {settings.max_proposal_qty_units:g} units.')
        if value>settings.max_proposal_value: reasons.append(f'Proposal value exceeds hard cap of ₹{settings.max_proposal_value:,.2f}.')
        if not product.reorder_enabled: reasons.append('Product is not enabled for procurement.')
        if supplier is None or not supplier.is_active: reasons.append('No active supplier is available.')
        if supplier and value<supplier.min_order_value: warnings.append(f'Below supplier minimum order value ₹{supplier.min_order_value:,.2f}.')
        if qty%pack!=0: reasons.append(f'Quantity must be a multiple of pack size {pack}.')
        return GuardrailResult(not reasons,reasons,warnings)
    def validate_approval(self,proposal:ProcurementProposal,product:Product,supplier:Supplier|None,requested_qty:float)->GuardrailResult:
        result=self.validate_proposal(product,supplier,requested_qty,proposal.unit_cost)
        if proposal.status!='PENDING': result.allowed=False; result.reasons.append(f'Proposal is in state {proposal.status}, not PENDING.')
        if proposal.expiry_risk_score>=0.50 and requested_qty>0: result.allowed=False; result.reasons.append('High expiry risk blocks a new purchase until expiry review.')
        return result
