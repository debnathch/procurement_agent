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
        self._index_cutoff: datetime | None = None
        self._sales_by_code: dict[str, tuple[float, set]] = {}
        self._sales_by_name: dict[str, tuple[float, set]] = {}
        self._sales_by_canonical: dict[str, tuple[float, set]] = {}
        self._products_by_code: dict[str, Product] = {}
        self._canonical_to_codes: dict[str, list[str]] = {}
        self._batch_stock_by_code: dict[str, float] = {}

    def invalidate_index(self) -> None:
        """Explicitly clear the in-memory cache."""
        self._index_cutoff = None

    def _ensure_index(self, cutoff: datetime, force: bool = False) -> None:
        """Index sales history, products, and batch stock in memory once for O(1) velocity lookups."""
        if self._index_cutoff == cutoff and not force:
            return

        from collections import defaultdict
        from backend.app.adapters.excel import pharma_canonical_key

        # 1. Index products
        products = self.db.scalars(select(Product)).all()
        self._products_by_code = {p.product_code: p for p in products}
        self._canonical_to_codes = defaultdict(list)
        for p in products:
            ck = pharma_canonical_key(p.product_name)
            self._canonical_to_codes[ck].append(p.product_code)

        # 2. Index sales history since cutoff
        sh_rows = self.db.execute(
            select(
                SalesHistory.product_code,
                SalesHistory.product_name,
                SalesHistory.sale_date,
                SalesHistory.qty_sold
            ).where(SalesHistory.sale_date >= cutoff)
        ).all()

        code_totals = defaultdict(float)
        code_dates = defaultdict(set)
        name_totals = defaultdict(float)
        name_dates = defaultdict(set)
        canonical_totals = defaultdict(float)
        canonical_dates = defaultdict(set)

        for p_code, p_name, s_date, qty in sh_rows:
            q = qty or 0.0
            if p_code:
                code_totals[p_code] += q
                if s_date:
                    code_dates[p_code].add(s_date)
            if p_name:
                c_name = p_name.strip().upper()
                name_totals[c_name] += q
                if s_date:
                    name_dates[c_name].add(s_date)
                ck = pharma_canonical_key(p_name)
                canonical_totals[ck] += q
                if s_date:
                    canonical_dates[ck].add(s_date)

        self._sales_by_code = {
            k: (code_totals[k], code_dates[k]) for k in code_totals
        }
        self._sales_by_name = {
            k: (name_totals[k], name_dates[k]) for k in name_totals
        }
        self._sales_by_canonical = {
            k: (canonical_totals[k], canonical_dates[k]) for k in canonical_totals
        }

        # 3. Index batch stock for stockout baselines
        batches = self.db.scalars(select(InventoryBatch)).all()
        batch_stocks = defaultdict(float)
        for b in batches:
            batch_stocks[b.product_code] += (b.qty_on_hand or 0.0)
        self._batch_stock_by_code = dict(batch_stocks)

        self._index_cutoff = cutoff

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

        self._ensure_index(cutoff)

        if product_code not in self._products_by_code:
            prod_exists = self.db.scalars(select(Product.id).where(Product.product_code == product_code)).first()
            if prod_exists:
                self._ensure_index(cutoff, force=True)

        # 1. Exact match by product_code
        stats = self._sales_by_code.get(product_code)
        if stats and stats[0] > 0 and len(stats[1]) > 0:
            avg_daily = stats[0] / elapsed_days
            return round(avg_daily, 4), source_suffix

        # Fallback 0: Check sales history by product_name and canonical formulation variants
        from backend.app.adapters.excel import pharma_canonical_key
        prod = self._products_by_code.get(product_code)
        if not prod:
            prod = self.db.scalars(select(Product).where(Product.product_code == product_code)).first()

        if prod and prod.product_name:
            clean_name = prod.product_name.strip().upper()
            # 0a. Match by exact product_name in SalesHistory (e.g. when sales report lacks internal codes)
            n_stats = self._sales_by_name.get(clean_name)
            if n_stats and n_stats[0] > 0 and len(n_stats[1]) > 0:
                avg_daily = n_stats[0] / elapsed_days
                return round(avg_daily, 4), source_suffix

            # 0b. Match by pharma_canonical_key across SalesHistory (canonical formulation fallback)
            c_key = pharma_canonical_key(prod.product_name)
            c_stats = self._sales_by_canonical.get(c_key)
            if c_stats and c_stats[0] > 0 and len(c_stats[1]) > 0:
                avg_daily = c_stats[0] / elapsed_days
                return round(avg_daily, 4), source_suffix

            # 0c. Check across alternate product codes with same canonical formulation key
            alt_codes = [c for c in self._canonical_to_codes.get(c_key, []) if c != product_code]
            if alt_codes:
                alt_total = 0.0
                alt_dates = set()
                for ac in alt_codes:
                    ac_stats = self._sales_by_code.get(ac)
                    if ac_stats:
                        alt_total += ac_stats[0]
                        alt_dates.update(ac_stats[1])
                if alt_total > 0 and len(alt_dates) > 0:
                    avg_daily = alt_total / elapsed_days
                    return round(avg_daily, 4), source_suffix

        # Fallback 1: estimate daily demand from reorder_point / 30 if available
        product = prod
        if product and product.reorder_point and product.reorder_point > 0:
            avg_daily = product.reorder_point / 30.0
            return round(avg_daily, 4), 'reorder_point_heuristic'

        # Fallback 2: Catalog items that are completely out of stock get baseline replenishment demand
        if product and product.reorder_enabled:
            total_on_hand = self._batch_stock_by_code.get(product_code, 0.0)
            if total_on_hand <= 0:
                baseline = max(1.0, product.min_order_qty, product.pack_size) / 30.0
                return round(baseline, 4), 'stockout_replenishment_baseline'

        return 0.0, 'no_history'
