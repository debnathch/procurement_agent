"""Inventory position service — aggregates InventoryBatch rows with FEFO / expiry awareness."""
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.app.models.entities import InventoryBatch
from backend.app.core.config import settings


class InventoryService:
    def __init__(self, db: Session):
        self.db = db

    def position(self, product_code: str, avg_daily_demand: float) -> dict:
        """
        Returns a dict with:
          on_hand, on_order, near_expiry, expired, usable_before_expiry,
          expiry_risk (0-1), expiry_action, batches (list)
        """
        now = datetime.utcnow()
        horizon = now + timedelta(days=settings.expiry_risk_horizon_days)

        batches = self.db.scalars(
            select(InventoryBatch).where(InventoryBatch.product_code == product_code)
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
