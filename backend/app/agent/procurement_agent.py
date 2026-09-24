import hashlib
import json
import uuid
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.app.models.entities import Product, Supplier, ProcurementProposal, ProcurementRun
from backend.app.services.forecast import DemandService
from backend.app.services.inventory import InventoryService
from backend.app.services.policy import ProcurementPolicy
from backend.app.services.supplier import SupplierService
from backend.app.services.guardrails import ProcurementGuardrails
from backend.app.services.audit import audit
from backend.app.core.config import settings

class ProcurementAgent:
    """Reusable bounded procurement domain agent."""
    name = 'marg-procurement-agent'
    version = '1.1.0'

    def __init__(self, db: Session):
        self.db = db
        self.demand = DemandService(db)
        self.inventory = InventoryService(db)
        self.suppliers = SupplierService(db)
        self.policy = ProcurementPolicy(settings.default_review_days, settings.default_safety_days, settings.expiry_risk_horizon_days)
        self.guardrails = ProcurementGuardrails()

    def run(self, product_codes: list[str] | None = None, lead_time_override: int | None = None):
        run_id = uuid.uuid4().hex
        stmt = select(Product).where(Product.reorder_enabled == True)
        if product_codes:
            stmt = stmt.where(Product.product_code.in_(product_codes))
        products = self.db.scalars(stmt).all()
        proposals = []
        for product in products:
            demand, demand_source = self.demand.forecast_daily(product.product_code)
            inv = self.inventory.position(product.product_code, demand)
            supplier = self.suppliers.choose(product)
            lead_time = lead_time_override or (supplier.lead_time_days if supplier and supplier.lead_time_days else settings.default_lead_time_days)
            unit_cost = product.unit_cost or (inv['batches'][0].unit_cost if inv['batches'] else 0.0)
            calc = self.policy.calculate(
                avg_daily_demand=demand, lead_time_days=lead_time,
                stock_on_hand=inv['on_hand'], stock_on_order=inv['on_order'],
                usable_before_expiry=inv['usable_before_expiry'],
                near_expiry_qty=inv['near_expiry'], min_order_qty=product.min_order_qty,
                pack_size=product.pack_size, unit_cost=unit_cost, expiry_risk=inv['expiry_risk'])
            if calc['order_qty'] <= 0:
                continue
            guard = self.guardrails.validate_proposal(product, supplier, calc['order_qty'], unit_cost)
            if not guard.allowed:
                audit(self.db, 'PROPOSAL_BLOCKED', entity_type='product', entity_id=product.product_code,
                      details={'reasons': guard.reasons})
                continue
            rationale = (
                f'Demand={demand:.2f}/day ({demand_source}); lead_time={lead_time}d; '
                f'on_hand={inv["on_hand"]:.0f}; on_order={inv["on_order"]:.0f}; '
                f'near_expiry={inv["near_expiry"]:.0f}; expired={inv["expired"]:.0f}; '
                f'FEFO_usable={inv["usable_before_expiry"]:.0f}; target={calc["target_stock"]:.0f}; '
                f'recommended={calc["order_qty"]:.0f}; expiry_risk={inv["expiry_risk"]:.2%}; '
                f'action={calc["expiry_action"]}.')
            idem = hashlib.sha256(f'{run_id}:{product.product_code}:{supplier.supplier_id if supplier else "NONE"}:{calc["order_qty"]}'.encode()).hexdigest()
            proposal = ProcurementProposal(
                run_id=run_id, product_code=product.product_code, product_name=product.product_name,
                supplier_id=supplier.supplier_id if supplier else None,
                supplier_name=supplier.supplier_name if supplier else None,
                recommended_qty=calc['order_qty'], unit_cost=unit_cost,
                estimated_value=calc['estimated_value'], avg_daily_demand=demand,
                demand_source=demand_source, lead_time_days=lead_time,
                stock_on_hand=inv['on_hand'], stock_on_order=inv['on_order'],
                near_expiry_qty=inv['near_expiry'], expired_qty=inv['expired'],
                expiry_risk_score=inv['expiry_risk'], expiry_action=calc['expiry_action'],
                rationale=rationale, status='PENDING', idempotency_key=idem)
            self.db.add(proposal)
            proposals.append(proposal)
        self.db.add(ProcurementRun(run_id=run_id, status='COMPLETED',
                                   product_count=len(products), proposal_count=len(proposals)))
        audit(self.db, 'PROCUREMENT_RUN_COMPLETED', entity_type='run', entity_id=run_id,
              details={'products': len(products), 'proposals': len(proposals)})
        self.db.commit()
        return run_id, proposals
