"""Deterministic procurement policy — calculates reorder quantity."""
import math


class ProcurementPolicy:
    def __init__(self, review_days: int = 7, safety_days: int = 3, expiry_risk_horizon_days: int = 90):
        self.review_days = review_days
        self.safety_days = safety_days
        self.expiry_risk_horizon_days = expiry_risk_horizon_days

    def calculate(
        self,
        avg_daily_demand: float,
        lead_time_days: int,
        stock_on_hand: float,
        stock_on_order: float,
        usable_before_expiry: float,
        near_expiry_qty: float,
        min_order_qty: float,
        pack_size: float,
        unit_cost: float,
        expiry_risk: float,
    ) -> dict:
        """
        Returns dict with: order_qty, target_stock, estimated_value, expiry_action.

        Formula:
          target = demand * (lead_time + review_days + safety_days)
          net_need = target - usable_before_expiry - on_order
          order_qty = round up to pack_size, at least min_order_qty
        """
        pack = max(1.0, pack_size)

        # Expiry action
        if expiry_risk >= 0.50:
            expiry_action = 'PAUSE_PROCUREMENT'
            return {
                'order_qty': 0.0,
                'target_stock': 0.0,
                'estimated_value': 0.0,
                'expiry_action': expiry_action,
            }
        elif expiry_risk >= 0.25:
            expiry_action = 'REDUCE_ORDER'
            demand_multiplier = 0.5
        else:
            expiry_action = 'NORMAL'
            demand_multiplier = 1.0

        target_stock = avg_daily_demand * demand_multiplier * (lead_time_days + self.review_days + self.safety_days)
        net_need = target_stock - usable_before_expiry - stock_on_order

        if net_need <= 0:
            return {
                'order_qty': 0.0,
                'target_stock': round(target_stock, 2),
                'estimated_value': 0.0,
                'expiry_action': expiry_action,
            }

        # Round up to pack size
        packs_needed = math.ceil(net_need / pack)
        order_qty = packs_needed * pack

        # Enforce minimum order quantity
        if order_qty < min_order_qty:
            order_qty_packs = math.ceil(min_order_qty / pack)
            order_qty = order_qty_packs * pack

        estimated_value = order_qty * unit_cost

        return {
            'order_qty': round(order_qty, 4),
            'target_stock': round(target_stock, 2),
            'estimated_value': round(estimated_value, 2),
            'expiry_action': expiry_action,
        }
