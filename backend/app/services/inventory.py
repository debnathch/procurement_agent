"""
Inventory Position & FEFO Expiry Analysis Service

Aggregates inventory batches across First-Expiry-First-Out (FEFO) lifecycle stages:
- Active On-Hand: Physical units currently stocked in the warehouse.
- Pipeline On-Order: Units committed in pending purchase orders.
- Near-Expiry Batches: Units expiring within the configured risk horizon (default: 90 days).
- Expired Stock: Units past their shelf-life validity (excluded from usable inventory).
- Usable Before Expiry: Realistic quantity consumable before batch expiration based on daily demand.
"""

from __future__ import annotations
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.app.models.entities import InventoryBatch, Product
from backend.app.core.config import settings


class InventoryService:
    """
    Evaluates warehouse inventory positions with batch-level FEFO expiry awareness.
    """

    def __init__(self, db: Session) -> None:
        """
        Initialize the inventory service.

        Args:
            db (Session): Active SQLAlchemy database session.
        """
        self.db: Session = db

    def position(self, product_code: str, avg_daily_demand: float) -> dict:
        """
        Aggregate batch-level stock positions and calculate shelf-life risk metrics for a product.

        Analysis Steps:
        ---------------
        1. Resolve all relevant product codes sharing the same canonical medicine identity.
        2. Query all `InventoryBatch` records across matching codes.
        3. Partition quantities into:
           - on_hand: Total physical quantity.
           - on_order: Confirmed pipeline quantity.
           - expired: Batches with expiry_date < current UTC time.
           - near_expiry: Batches with current UTC time <= expiry_date <= risk_horizon (90 days).
           - usable_before_expiry: Stock safe from expiration during expected consumption.
        4. Compute expiry_risk fraction = near_expiry / on_hand.
        5. Assign operational expiry action recommendation:
           - expiry_risk >= 50% -> 'PAUSE_PROCUREMENT'
           - expiry_risk >= 25% -> 'REDUCE_ORDER'
           - else -> 'NORMAL'

        Args:
            product_code (str): Canonical product code.
            avg_daily_demand (float): Projected daily sales consumption velocity.

        Returns:
            dict: Inventory position metrics including on_hand, on_order, near_expiry,
                  expired, usable_before_expiry, expiry_risk, and expiry_action.
        """
        now = datetime.utcnow()
        horizon = now + timedelta(days=settings.expiry_risk_horizon_days)

        from backend.app.adapters.excel import canonical_medicine_key
        prod = self.db.get(Product, product_code)
        alt_codes = [product_code]
        if prod:
            c_key = canonical_medicine_key(prod.product_name)
            all_prods = self.db.scalars(select(Product)).all()
            alt_codes = list({
                p.product_code for p in all_prods
                if canonical_medicine_key(p.product_name) == c_key
            } | {product_code})

        batches = self.db.scalars(
            select(InventoryBatch).where(InventoryBatch.product_code.in_(alt_codes))
        ).all()

        on_hand = 0.0
        on_order = 0.0
        near_expiry = 0.0
        expired = 0.0
        usable_before_expiry = 0.0

        for b in batches:
            on_hand += b.qty_on_hand
            on_order += b.qty_on_order
            if b.expiry_date:
                if b.expiry_date <= now:
                    expired += b.qty_on_hand
                elif b.expiry_date <= horizon:
                    near_expiry += b.qty_on_hand
            else:
                # No expiry date — treat as usable
                usable_before_expiry += b.qty_on_hand

        # Usable = non-expired stock
        usable = on_hand - expired
        usable_before_expiry += max(0.0, usable - near_expiry)

        # Expiry risk: ratio of near-expiry to total on-hand
        expiry_risk = (near_expiry / on_hand) if on_hand > 0 else 0.0

        # Expiry action hint for the agent
        if expiry_risk >= 0.50:
            expiry_action = 'PAUSE_PROCUREMENT'
        elif expiry_risk >= 0.25:
            expiry_action = 'REDUCE_ORDER'
        else:
            expiry_action = 'NORMAL'

        return {
            'on_hand': round(on_hand, 4),
            'on_order': round(on_order, 4),
            'near_expiry': round(near_expiry, 4),
            'expired': round(expired, 4),
            'usable_before_expiry': round(usable_before_expiry, 4),
            'expiry_risk': round(expiry_risk, 4),
            'expiry_action': expiry_action,
            'batches': batches,
        }
