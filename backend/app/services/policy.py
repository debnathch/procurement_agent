"""
Deterministic Procurement Policy Engine

Implements pharmaceutical inventory replenishment mathematics combining:
1. Periodic Review Model: Target Stock = Demand * (Lead Time + Review Cycle + Safety Buffer).
2. FEFO Expiry Risk Modulation: Throttling or pausing procurement for batches at risk of expiring.
3. Pack-Size Discretization: Ceiling round-up to manufacturer box/strip multiples.
4. MOQ Hurdle Enforcement: Ensuring order quantities satisfy vendor minimum requirements.
"""

from __future__ import annotations
import math


class ProcurementPolicy:
    """
    Deterministic inventory coverage and reorder quantity calculator.

    Attributes:
        review_days (int): Interval between procurement review cycles (default: 7 days).
        safety_days (int): Buffer days to protect against supplier stockouts or transit delays (default: 3 days).
        expiry_risk_horizon_days (int): Lookahead window to identify near-expiry batches (default: 90 days).
    """

    def __init__(
        self,
        review_days: int = 7,
        safety_days: int = 3,
        expiry_risk_horizon_days: int = 90,
    ) -> None:
        """
        Initialize policy calculation parameters.

        Args:
            review_days (int): Reorder review cycle interval in days.
            safety_days (int): Safety stock buffer in days.
            expiry_risk_horizon_days (int): Window in days for classifying near-expiry risk.
        """
        self.review_days: int = review_days
        self.safety_days: int = safety_days
        self.expiry_risk_horizon_days: int = expiry_risk_horizon_days

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
    ) -> dict[str, float | str]:
        """
        Compute the suggested purchase quantity and safety status for a single product.

        Mathematical Formulas:
        ----------------------
        1. Expiry Risk Modulation:
           - If expiry_risk >= 50%: Action = 'PAUSE_PROCUREMENT', Order Quantity = 0.
           - If 25% <= expiry_risk < 50%: Action = 'REDUCE_ORDER', Demand Multiplier = 0.5.
           - Else: Action = 'NORMAL', Demand Multiplier = 1.0.

        2. Target Stock:
           Target = (Demand * Demand_Multiplier) * (Lead_Time + Review_Days + Safety_Days)

        3. Net Need:
           Net_Need = Target - Usable_Stock - Stock_On_Order

        4. Pack Rounding & MOQ:
           If Net_Need > 0:
             Order_Qty = ceil(Net_Need / Pack_Size) * Pack_Size
             If Order_Qty < MOQ:
               Order_Qty = ceil(MOQ / Pack_Size) * Pack_Size

        Args:
            avg_daily_demand (float): Average daily units sold (D).
            lead_time_days (int): Vendor lead time in days (L).
            stock_on_hand (float): Total physical inventory units in warehouse.
            stock_on_order (float): Units in pending purchase orders already dispatched.
            usable_before_expiry (float): Inventory units with shelf-life exceeding consumption horizon.
            near_expiry_qty (float): Inventory units expiring within the risk horizon.
            min_order_qty (float): Minimum order quantity (MOQ) enforced by supplier.
            pack_size (float): Units per packaging container/strip/box.
            unit_cost (float): Purchase rate per unit (INR).
            expiry_risk (float): Fraction of inventory expiring within the horizon (0.0 to 1.0).

        Returns:
            dict[str, float | str]: Dictionary containing:
                - 'order_qty' (float): Final suggested reorder quantity.
                - 'target_stock' (float): Target inventory position in units.
                - 'net_need' (float): Raw deficit before packaging discretization.
                - 'estimated_value' (float): Projected purchase order value (Qty * Cost).
                - 'expiry_action' (str): Risk action ('NORMAL', 'REDUCE_ORDER', 'PAUSE_PROCUREMENT').
        """
        pack = max(1.0, pack_size)

        # Expiry action
        if expiry_risk >= 0.50:
            expiry_action = 'PAUSE_PROCUREMENT'
            return {
                'order_qty': 0.0,
                'target_stock': 0.0,
                'net_need': 0.0,
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
                'net_need': round(net_need, 2),
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
            'net_need': round(net_need, 2),
            'estimated_value': round(estimated_value, 2),
            'expiry_action': expiry_action,
        }
