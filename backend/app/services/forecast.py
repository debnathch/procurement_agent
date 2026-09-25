"""
Demand Forecasting Service

Computes average daily consumption rate ($D$) for pharmaceuticals.
Implements a 4-tier fallback hierarchy:
1. Exact Sales History: Analyzes trailing lookback days (default: 90 days) for the product.
2. Canonical Medicine Matching: Matches across spelling, packaging, or brand variants.
3. Reorder Point Heuristic: Derives velocity from safety reorder point ($ROP / 30$).
4. Stockout Replenishment Baseline: Ensures catalog items with 0 stock receive baseline ordering.
"""

from __future__ import annotations
from datetime import datetime, timedelta
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from backend.app.models.entities import SalesHistory, Product, InventoryBatch


class DemandService:
    """
    Evaluates historical consumption velocity to estimate daily replenishment demand.
    """

    def __init__(self, db: Session) -> None:
        """
        Initialize the demand forecasting service.

        Args:
            db (Session): Active SQLAlchemy database session.
        """
        self.db: Session = db

    def forecast_daily(self, product_code: str, lookback_days: int = 90) -> tuple[float, str]:
        """
        Calculate the average daily sales velocity and source attribution for a product.

        Evaluation Hierarchy:
        ---------------------
        1. Query `SalesHistory` for `product_code` in the trailing `lookback_days` window.
           If total units > 0 and days with sales > 0:
           Velocity = Total_Sold / Lookback_Days, Source = 'sales_history_90d'.
        2. If 0 sales, query sales across products with the same canonical medicine key
           (e.g., matching 'AC-BEN SP TABLET 10X10' and 'AC-BEN SP TABLET').
        3. If no sales history exists, derive from `product.reorder_point / 30.0`
           Source = 'reorder_point_heuristic'.
        4. If the product is out of stock (0 units on hand), assign a baseline:
           max(1.0, MOQ, Pack_Size) / 30.0, Source = 'stockout_replenishment_baseline'.
        5. If none apply, return 0.0 with Source = 'no_history'.

        Args:
            product_code (str): The unique canonical product code.
            lookback_days (int): Historical sales lookback window (default: 90 days).

        Returns:
            tuple[float, str]: A tuple of (average_daily_demand, source_attribution_label).
        """
        cutoff = datetime.utcnow() - timedelta(days=lookback_days)

        result = self.db.execute(
            select(
                func.sum(SalesHistory.qty_sold).label('total_sold'),
                func.count(SalesHistory.sale_date.distinct()).label('days_with_sales'),
            ).where(
                SalesHistory.product_code == product_code,
                SalesHistory.sale_date >= cutoff,
            )
        ).one()

        total_sold = result.total_sold or 0.0
        days_with_sales = result.days_with_sales or 0

        if total_sold > 0 and days_with_sales > 0:
            avg_daily = total_sold / lookback_days
            return round(avg_daily, 4), f'sales_history_{lookback_days}d'

        # Fallback 0: Check sales history across canonical product name variants
        from backend.app.adapters.excel import canonical_medicine_key
        prod = self.db.get(Product, product_code)
        if prod:
            c_key = canonical_medicine_key(prod.product_name)
            all_prods = self.db.scalars(select(Product)).all()
            alt_codes = [
                p.product_code for p in all_prods
                if canonical_medicine_key(p.product_name) == c_key and p.product_code != product_code
            ]
            if alt_codes:
                alt_res = self.db.execute(
                    select(
                        func.sum(SalesHistory.qty_sold).label('total_sold'),
                        func.count(SalesHistory.sale_date.distinct()).label('days_with_sales'),
                    ).where(
                        SalesHistory.product_code.in_(alt_codes),
                        SalesHistory.sale_date >= cutoff,
                    )
                ).one()
                alt_total = alt_res.total_sold or 0.0
                alt_days = alt_res.days_with_sales or 0
                if alt_total > 0 and alt_days > 0:
                    avg_daily = alt_total / lookback_days
                    return round(avg_daily, 4), f'sales_history_{lookback_days}d'

        # Fallback 1: estimate daily demand from reorder_point / 30 if available
        product = self.db.get(Product, product_code)
        if product and product.reorder_point and product.reorder_point > 0:
            avg_daily = product.reorder_point / 30.0
            return round(avg_daily, 4), 'reorder_point_heuristic'

        # Fallback 2: Catalog items that are completely out of stock get baseline replenishment demand
        if product and product.reorder_enabled:
            batches = self.db.scalars(select(InventoryBatch).where(InventoryBatch.product_code == product_code)).all()
            total_on_hand = sum(b.qty_on_hand for b in batches)
            if total_on_hand <= 0:
                baseline = max(1.0, product.min_order_qty, product.pack_size) / 30.0
                return round(baseline, 4), 'stockout_replenishment_baseline'

        return 0.0, 'no_history'
