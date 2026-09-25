"""
SQLAlchemy ORM entities for the MARG Procurement Agent.

Tables:
  - products          : canonical product master
  - suppliers         : supplier master
  - inventory_batches : per-batch stock with expiry (FEFO)
  - sales_history     : daily sales lines for demand forecasting
  - procurement_runs  : one row per agent run
  - procurement_proposals : one proposal per product per run
  - feedback_events   : human approve/reject/modify decisions
  - audit_events      : immutable audit trail
"""
from datetime import datetime
from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from backend.app.core.database import Base


# ---------------------------------------------------------------------------
# Product master
# ---------------------------------------------------------------------------
class Product(Base):
    """
    Canonical product catalog master record.

    Stores packaging configurations, purchase costs, inventory thresholds,
    and foreign key linkage to preferred vendors.
    """
    __tablename__ = 'products'

    product_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_name: Mapped[str] = mapped_column(String(256), nullable=False)
    category: Mapped[str | None] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(String(32), default='units')
    pack_size: Mapped[float] = mapped_column(Float, default=1.0)
    min_order_qty: Mapped[float] = mapped_column(Float, default=1.0)
    unit_cost: Mapped[float | None] = mapped_column(Float)
    reorder_point: Mapped[float] = mapped_column(Float, default=0.0)
    reorder_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    preferred_supplier_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    batches: Mapped[list['InventoryBatch']] = relationship(back_populates='product', cascade='all, delete-orphan')
    sales: Mapped[list['SalesHistory']] = relationship(back_populates='product', cascade='all, delete-orphan')


# ---------------------------------------------------------------------------
# Supplier master
# ---------------------------------------------------------------------------
class Supplier(Base):
    """
    Vendor and manufacturer master record.

    Holds commercial terms, delivery lead times in days, minimum order value thresholds,
    and dynamic reliability scores used for multi-criteria vendor ranking.
    """
    __tablename__ = 'suppliers'

    supplier_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    supplier_name: Mapped[str] = mapped_column(String(256), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(128))
    contact_email: Mapped[str | None] = mapped_column(String(256))
    contact_phone: Mapped[str | None] = mapped_column(String(64))
    lead_time_days: Mapped[int] = mapped_column(Integer, default=45)
    min_order_value: Mapped[float] = mapped_column(Float, default=0.0)
    reliability_score: Mapped[float] = mapped_column(Float, default=1.0)  # 0-1
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Inventory batches (FEFO support)
# ---------------------------------------------------------------------------
class InventoryBatch(Base):
    """
    Batch-level inventory record enabling First-Expiry-First-Out (FEFO) tracking.

    Records batch identifiers, physical stock on hand, pipeline orders, and pharmaceutical expiry dates.
    """
    __tablename__ = 'inventory_batches'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_code: Mapped[str] = mapped_column(String(64), ForeignKey('products.product_code'), nullable=False)
    batch_no: Mapped[str | None] = mapped_column(String(64))
    qty_on_hand: Mapped[float] = mapped_column(Float, default=0.0)
    qty_on_order: Mapped[float] = mapped_column(Float, default=0.0)
    expiry_date: Mapped[datetime | None] = mapped_column(DateTime)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    location: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    product: Mapped['Product'] = relationship(back_populates='batches')

    __table_args__ = (
        Index('ix_inventory_batches_product_code', 'product_code'),
    )


# ---------------------------------------------------------------------------
# Sales history (for demand forecasting)
# ---------------------------------------------------------------------------
class SalesHistory(Base):
    """
    Historical sales line record capturing actual consumption over time.

    Used by DemandService to compute average daily demand velocity.
    """
    __tablename__ = 'sales_history'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_code: Mapped[str] = mapped_column(String(64), ForeignKey('products.product_code'), nullable=False)
    sale_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    qty_sold: Mapped[float] = mapped_column(Float, default=0.0)
    channel: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    product: Mapped['Product'] = relationship(back_populates='sales')

    __table_args__ = (
        Index('ix_sales_history_product_date', 'product_code', 'sale_date'),
    )


# ---------------------------------------------------------------------------
# Procurement run (one per agent invocation)
# ---------------------------------------------------------------------------
class ProcurementRun(Base):
    """
    Operational record tracking a single end-to-end execution of the Procurement Agent.
    """
    __tablename__ = 'procurement_runs'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default='RUNNING')  # RUNNING | COMPLETED | FAILED
    product_count: Mapped[int] = mapped_column(Integer, default=0)
    proposal_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


# ---------------------------------------------------------------------------
# Procurement proposal (one per product per run)
# ---------------------------------------------------------------------------
class ProcurementProposal(Base):
    """
    Replenishment order recommendation generated by the agent.

    Awaits human-in-the-loop review, adjustment, and approval before PO execution.
    """
    __tablename__ = 'procurement_proposals'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_code: Mapped[str] = mapped_column(String(64), nullable=False)
    product_name: Mapped[str] = mapped_column(String(256), nullable=False)
    supplier_id: Mapped[str | None] = mapped_column(String(64))
    supplier_name: Mapped[str | None] = mapped_column(String(256))

    recommended_qty: Mapped[float] = mapped_column(Float, nullable=False)
    approved_qty: Mapped[float | None] = mapped_column(Float)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_value: Mapped[float] = mapped_column(Float, default=0.0)

    avg_daily_demand: Mapped[float] = mapped_column(Float, default=0.0)
    demand_source: Mapped[str | None] = mapped_column(String(64))
    lead_time_days: Mapped[int] = mapped_column(Integer, default=45)
    stock_on_hand: Mapped[float] = mapped_column(Float, default=0.0)
    stock_on_order: Mapped[float] = mapped_column(Float, default=0.0)
    near_expiry_qty: Mapped[float] = mapped_column(Float, default=0.0)
    expired_qty: Mapped[float] = mapped_column(Float, default=0.0)
    expiry_risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    expiry_action: Mapped[str | None] = mapped_column(String(64))

    rationale: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(64), default='PENDING')
    # PENDING | APPROVED_PENDING_EXECUTION | EXECUTED | REJECTED

    human_reason: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime)
    execution_reference: Mapped[str | None] = mapped_column(String(256))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    feedback: Mapped[list['FeedbackEvent']] = relationship(back_populates='proposal', cascade='all, delete-orphan')

    __table_args__ = (
        Index('ix_proposals_run_id', 'run_id'),
        Index('ix_proposals_status', 'status'),
    )


# ---------------------------------------------------------------------------
# Feedback events (human decisions)
# ---------------------------------------------------------------------------
class FeedbackEvent(Base):
    """
    Audit log of human-in-the-loop decisions (approve, modify, reject).
    """
    __tablename__ = 'feedback_events'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id: Mapped[int] = mapped_column(Integer, ForeignKey('procurement_proposals.id'), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)  # APPROVE | REJECT | MODIFY_APPROVE
    original_qty: Mapped[float | None] = mapped_column(Float)
    final_qty: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(128), default='human-ui')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    proposal: Mapped['ProcurementProposal'] = relationship(back_populates='feedback')


# ---------------------------------------------------------------------------
# Audit events (immutable trail)
# ---------------------------------------------------------------------------
class AuditEvent(Base):
    """
    Regulatory and compliance append-only audit trail logging all system and human events.
    """
    __tablename__ = 'audit_events'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), default='system')
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(128))
    details_json: Mapped[str] = mapped_column(Text, default='{}')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index('ix_audit_events_event_type', 'event_type'),
        Index('ix_audit_events_entity', 'entity_type', 'entity_id'),
    )
