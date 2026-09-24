"""
Supplier Selection Service

Selects the best supplier for a given product based on:
1. Preferred supplier assignment (if designated on the Product record).
2. Multi-criteria ranking across all active suppliers:
   - Highest reliability score (descending)
   - Shortest delivery lead time (ascending)
   - Lowest minimum order value threshold (ascending)
"""

from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.app.models.entities import Product, Supplier


class SupplierService:
    """
    Evaluates and chooses the optimal vendor for pharmaceutical replenishment.
    """

    def __init__(self, db: Session):
        """
        Initialize SupplierService with database session.

        Args:
            db (Session): Active SQLAlchemy database session.
        """
        self.db = db

    def choose(self, product: Product) -> Supplier | None:
        """
        Choose the best active supplier for a product.

        Logic:
        1. Fetch all suppliers where `is_active == True`.
        2. If the product has a `preferred_supplier_id` that is active, choose it immediately.
        3. Otherwise, rank active suppliers by:
           - Reliability score (-s.reliability_score -> highest first)
           - Lead time in days (s.lead_time_days -> fastest first)
           - Minimum order value (s.min_order_value -> lowest hurdle first)

        Args:
            product (Product): Product record needing procurement.

        Returns:
            Supplier | None: Selected Supplier record, or None if no active suppliers exist.
        """
        # Retrieve all active suppliers
        stmt = select(Supplier).where(Supplier.is_active == True)
        suppliers = self.db.scalars(stmt).all()

        if not suppliers:
            return None

        # Priority 1: Check if product has an active preferred supplier assigned
        if product.preferred_supplier_id:
            preferred = [s for s in suppliers if s.supplier_id == product.preferred_supplier_id]
            if preferred:
                return preferred[0]

        # Priority 2: Multi-criteria sort (best reliability, lowest lead time, lowest MOQ value)
        sorted_suppliers = sorted(
            suppliers,
            key=lambda s: (-s.reliability_score, s.lead_time_days, s.min_order_value)
        )

        return sorted_suppliers[0]
