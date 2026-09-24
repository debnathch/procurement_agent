"""Demand forecasting service — uses SalesHistory if available, falls back to reorder_point."""
from datetime import datetime, timedelta
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from backend.app.models.entities import SalesHistory, Product


class DemandService:
    def __init__(self, db: Session):
        self.db = db

    def forecast_daily(self, product_code: str, lookback_days: int = 90) -> tuple[float, str]:
        """
        Returns (avg_daily_demand, source_label).
        Priority: sales history → reorder_point heuristic → 0.
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

        # Fallback: estimate daily demand from reorder_point / 30 if available
        product = self.db.get(Product, product_code)
        if product and product.reorder_point and product.reorder_point > 0:
            avg_daily = product.reorder_point / 30.0
            return round(avg_daily, 4), 'reorder_point_heuristic'

        return 0.0, 'no_history'
