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
from datetime import datetime, date
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from backend.app.models.entities import SalesHistory, Product, InventoryBatch


def get_days_from_fiscal_year_start(ref_date: datetime | date | None = None) -> tuple[int, date]:
    """
    Computes the number of elapsed days from 1st April of the current financial year to the reference date (today).

    Formula:
        Daily Demand Velocity = (Total Quantity Sale) / (Days from 1st April of current year to today)

    - If today is on or after April 1st of year Y (e.g. Oct 2026): start is April 1, 2026.
    - If today is before April 1st of year Y (e.g. Feb 2027): start is April 1, 2026.
    - Minimum returned elapsed days is 1 (to prevent division by zero).

    Returns:
        tuple[int, date]: (elapsed_days, fiscal_year_start_date)
    """
    if ref_date is None:
        ref_date = datetime.utcnow().date()
    elif isinstance(ref_date, datetime):
        ref_date = ref_date.date()

    year = ref_date.year if ref_date.month >= 4 else ref_date.year - 1
    fy_start = date(year, 4, 1)
    elapsed_days = max(1, (ref_date - fy_start).days)
    return elapsed_days, fy_start


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

    def forecast_daily(
        self,
        product_code: str,
        as_of_date: datetime | date | None = None,
        lookback_days: int | None = None
    ) -> tuple[float, str]:
        """
        Calculate average daily sales velocity as:
            Daily Demand Velocity = (Total Quantity Sale) / (Days from 1st April of current year to today)

        Evaluation Hierarchy:
        ---------------------
        1. Query `SalesHistory` for `product_code` from 1st April of the current financial year to today.
           If total units > 0 and days with sales > 0:
           Velocity = Total_Sold / Days_From_1st_April.
        2. If 0 sales, query sales across products with the same canonical medicine key
           (e.g., matching 'AC-BEN SP TABLET 10X10' and 'AC-BEN SP TABLET').
        3. If no sales history exists, derive from `product.reorder_point / 30.0`
           Source = 'reorder_point_heuristic'.
        4. If the product is out of stock (0 units on hand), assign a baseline:
           max(1.0, MOQ, Pack_Size) / 30.0, Source = 'stockout_replenishment_baseline'.
        5. If none apply, return 0.0 with Source = 'no_history'.

        Args:
            product_code (str): The unique canonical product code.
            as_of_date (datetime | date | None): Optional evaluation reference date (default: today).
            lookback_days (int | None): Optional override for lookback days if explicitly specified.

        Returns:
            tuple[float, str]: A tuple of (average_daily_demand, source_attribution_label).
        """
        if lookback_days is not None and lookback_days > 0:
            elapsed_days = lookback_days
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(days=lookback_days)
            source_suffix = f'sales_history_{lookback_days}d'
        else:
            elapsed_days, fy_start = get_days_from_fiscal_year_start(as_of_date)
            cutoff = datetime(fy_start.year, fy_start.month, fy_start.day)
            source_suffix = f'sales_since_1st_april_{fy_start.year} ({elapsed_days}d)'

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
            avg_daily = total_sold / elapsed_days
            return round(avg_daily, 4), source_suffix

        # Fallback 0: Check sales history by product_name and canonical formulation variants
        from backend.app.adapters.excel import pharma_canonical_key
        prod = self.db.scalars(select(Product).where(Product.product_code == product_code)).first()
        if prod and prod.product_name:
            clean_name = prod.product_name.strip().upper()
            # 0a. Match by exact product_name in SalesHistory (e.g. when sales report lacks internal codes)
            name_res = self.db.execute(
                select(
                    func.sum(SalesHistory.qty_sold).label('total_sold'),
                    func.count(SalesHistory.sale_date.distinct()).label('days_with_sales'),
                ).where(
                    func.upper(SalesHistory.product_name) == clean_name,
                    SalesHistory.sale_date >= cutoff,
                )
            ).one()
            name_total = name_res.total_sold or 0.0
            name_days = name_res.days_with_sales or 0
            if name_total > 0 and name_days > 0:
                avg_daily = name_total / elapsed_days
                return round(avg_daily, 4), source_suffix

            # 0b. Match by pharma_canonical_key across SalesHistory (canonical formulation fallback)
            c_key = pharma_canonical_key(prod.product_name)
            all_sh = self.db.scalars(select(SalesHistory).where(SalesHistory.sale_date >= cutoff)).all()
            matched_sh = [s for s in all_sh if s.product_name and pharma_canonical_key(s.product_name) == c_key]
            if matched_sh:
                c_total = sum(s.qty_sold for s in matched_sh)
                c_days = len({s.sale_date for s in matched_sh if s.sale_date})
                if c_total > 0 and c_days > 0:
                    avg_daily = c_total / elapsed_days
                    return round(avg_daily, 4), source_suffix

            # 0c. Check across alternate product codes with same canonical formulation key
            all_prods = self.db.scalars(select(Product)).all()
            alt_codes = [
                p.product_code for p in all_prods
                if pharma_canonical_key(p.product_name) == c_key and p.product_code != product_code
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
                    avg_daily = alt_total / elapsed_days
                    return round(avg_daily, 4), source_suffix

        # Fallback 1: estimate daily demand from reorder_point / 30 if available
        product = prod
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
