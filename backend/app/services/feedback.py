"""
Human Feedback & Proposal Decision Service

Handles the Human-in-the-Loop review lifecycle:
1. Approving an agent proposal (either as-is or with modified quantity/supplier).
2. Rejecting a proposal with reviewer reasoning.
3. Recording an immutable FeedbackEvent memory for auditing.
4. Calling the Execution Adapter (DryRun, CSV, or live MARG API) to finalize the order.
"""

from datetime import datetime
from sqlalchemy.orm import Session

from backend.app.models.entities import ProcurementProposal, FeedbackEvent, Product, Supplier
from backend.app.adapters.executor import build_executor
from backend.app.services.guardrails import ProcurementGuardrails
from backend.app.services.audit import audit
from backend.app.core.config import settings


class ProposalService:
    """
    Service responsible for executing human decisions on procurement proposals.
    """

    def __init__(self, db: Session):
        """
        Initialize the service with a database session and guardrails engine.

        Args:
            db (Session): SQLAlchemy database session.
        """
        self.db = db
        self.guardrails = ProcurementGuardrails()

    def decide(
        self,
        proposal_id: int,
        action: str,
        approved_qty: float | None = None,
        supplier_id: str | None = None,
        reason: str | None = None,
        actor: str = "human-ui",
    ) -> dict:
        """
        Execute a human reviewer's decision on a proposal (Approve, Modify & Approve, or Reject).

        Workflow:
        1. Fetch proposal and corresponding product record.
        2. If 'reject':
           - Set status to 'REJECTED'.
           - Record feedback and audit events.
           - Return rejected status.
        3. If 'approve':
           - Validate supplier and policy permissions (e.g. allow_supplier_change).
           - Run guardrail validation on requested quantity and supplier.
           - Mark status 'APPROVED_PENDING_EXECUTION'.
           - Call executor (DryRun, CSV, or MARG ERP).
           - Update proposal with execution reference and final status ('EXECUTED').
           - Record audit log.

        Args:
            proposal_id (int): Primary key ID of the ProcurementProposal to decide.
            action (str): Decision action - either 'approve' or 'reject'.
            approved_qty (float | None): Corrected quantity (defaults to recommended_qty if None).
            supplier_id (str | None): Corrected supplier ID (defaults to proposed supplier if None).
            reason (str | None): Justification or note provided by human reviewer.
            actor (str): Identifier of the user or system taking action (e.g. 'human-reviewer').

        Returns:
            dict: Summary containing final status, execution reference, and message.

        Raises:
            ValueError: If proposal is not found, invalid action is passed, or guardrails fail.
        """
        # Step 1: Retrieve proposal and product records from database
        proposal = self.db.get(ProcurementProposal, proposal_id)
        if not proposal:
            raise ValueError(f"Proposal #{proposal_id} not found.")

        product = self.db.get(Product, proposal.product_code)
        if not product:
            raise ValueError(f"Product '{proposal.product_code}' for proposal #{proposal_id} no longer exists.")

        # Step 2: Handle Rejection
        if action == "reject":
            if proposal.status != "PENDING":
                raise ValueError(f"Cannot reject proposal in state '{proposal.status}' (must be PENDING).")

            proposal.status = "REJECTED"
            proposal.human_reason = reason or "Rejected by human reviewer."

            # Record feedback event for system learning and analytics
            self.db.add(FeedbackEvent(
                proposal_id=proposal.id,
                action="REJECT",
                original_qty=proposal.recommended_qty,
                final_qty=None,
                reason=proposal.human_reason,
                actor=actor,
            ))

            # Record immutable audit trail
            audit(
                self.db,
                event_type="PROPOSAL_REJECTED",
                actor=actor,
                entity_type="proposal",
                entity_id=str(proposal.id),
                details={"reason": proposal.human_reason},
            )

            self.db.commit()
            return {"status": proposal.status, "reference": "", "message": "Proposal rejected."}

        # Step 3: Handle Approval
        if action != "approve":
            raise ValueError(f"Unsupported decision action '{action}'. Supported actions: 'approve', 'reject'.")

        # Determine finalized quantity (defaulting to recommended quantity if none supplied)
        qty = float(approved_qty if approved_qty is not None else proposal.recommended_qty)

        # Determine finalized supplier
        sid = supplier_id or proposal.supplier_id
        supplier = self.db.get(Supplier, sid) if sid else None

        if supplier is None:
            raise ValueError("An active supplier is required to approve this proposal.")

        # Check policy: Is supplier override allowed?
        if not settings.allow_supplier_change and sid != proposal.supplier_id:
            raise ValueError("Changing supplier is disabled by policy (ALLOW_SUPPLIER_CHANGE=false).")

        # Step 4: Run guardrail validation on human input
        guard = self.guardrails.validate_approval(proposal, product, supplier, qty)
        if not guard.allowed:
            raise ValueError("Guardrail blocked approval: " + " | ".join(guard.reasons))

        # Step 5: Update proposal with human decisions
        proposal.approved_qty = qty
        proposal.supplier_id = supplier.supplier_id
        proposal.supplier_name = supplier.supplier_name
        proposal.human_reason = reason or "Approved by human reviewer."
        proposal.approved_at = datetime.utcnow()
        proposal.status = "APPROVED_PENDING_EXECUTION"

        # Record whether approved as-is or modified
        decision_type = "APPROVE" if qty == proposal.recommended_qty else "MODIFY_APPROVE"
        self.db.add(FeedbackEvent(
            proposal_id=proposal.id,
            action=decision_type,
            original_qty=proposal.recommended_qty,
            final_qty=qty,
            reason=proposal.human_reason,
            actor=actor,
        ))

        audit(
            self.db,
            event_type="PROPOSAL_APPROVED",
            actor=actor,
            entity_type="proposal",
            entity_id=str(proposal.id),
            details={"qty": qty, "supplier_id": supplier.supplier_id, "reason": proposal.human_reason},
        )
        self.db.commit()

        # Step 6: Dispatch order to active executor (DryRun, CSV, or live MARG ERP API)
        executor = build_executor()
        result = executor.create_purchase_order(
            supplier_id=supplier.supplier_id,
            company_code=settings.marg_company_code,
            items=[{
                "product_code": proposal.product_code,
                "quantity": qty,
                "unit_cost": proposal.unit_cost,
            }],
            remark=proposal.human_reason,
            idempotency_key=proposal.idempotency_key,
        )

        # Step 7: Update execution reference and final status
        proposal.status = "EXECUTED" if result.success else "APPROVED_PENDING_EXECUTION"
        if result.success:
            proposal.execution_reference = result.reference
            proposal.executed_at = datetime.utcnow()

        audit(
            self.db,
            event_type="PURCHASE_ORDER_EXECUTED" if result.success else "PURCHASE_ORDER_EXECUTION_FAILED",
            actor="system",
            entity_type="proposal",
            entity_id=str(proposal.id),
            details={"reference": result.reference, "message": result.message},
        )

        self.db.commit()
        return {
            "status": proposal.status,
            "reference": result.reference,
            "message": result.message,
        }
