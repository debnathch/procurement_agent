"""
Sales Intelligence & Customer Analytics Service.

Provides:
- Customer master lookup & aggregation
- Customer financial summaries (Total Sales, Sales 30d/90d, Current Dues, Ageing)
- Historical purchase behavior & order tracking
- Near-expiry customer product matching & reorder recommendations
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Optional
from collections import defaultdict
from sqlalchemy import func, select, desc
from sqlalchemy.orm import Session

from backend.app.models.entities import (
    Customer, CustomerReceivable, SalesHistory, InventoryBatch, Product
)
from backend.app.adapters.excel import (
    pharma_canonical_key, customer_canonical_key, normalize_customer_name
)
from backend.app.core.config import settings

logger = logging.getLogger(__name__)


class SalesIntelligenceService:
    def __init__(self, db: Session):
        self.db = db

    def list_customer_groups(self) -> list[str]:
        """Returns distinct customer groups/categories in the database."""
        stmt = select(Customer.group_name).where(Customer.group_name.is_not(None)).distinct()
        groups = [g for g in self.db.scalars(stmt).all() if g and str(g).strip()]
        return sorted(list(set(groups)))

    def list_customers(
        self,
        search: Optional[str] = None,
        group: Optional[str] = None,
        zero_sales_dues_gt_zero: bool = False,
        limit: int = 5000
    ) -> list[dict[str, Any]]:
        """
        Retrieves all customers with aggregated total sales and current dues.
        If zero_sales_dues_gt_zero is True, filters to parties with total_sales == 0 and current_dues > 0.
        If no Customer records exist yet, synthesizes customer list from sales_history.
        """
        now = datetime.utcnow()
        stmt = select(Customer)
        if group and str(group).strip() and str(group).strip() != "All":
            stmt = stmt.where(Customer.group_name == str(group).strip())
        if search:
            s_clean = f"%{search.strip().lower()}%"
            stmt = stmt.where(
                func.lower(Customer.customer_name).like(s_clean) |
                func.lower(Customer.customer_code).like(s_clean)
            )
        stmt = stmt.order_by(Customer.customer_name.asc()).limit(limit)
        customers = self.db.scalars(stmt).all()

        # If customers table is populated
        if customers:
            c_codes = [c.customer_code for c in customers]

            # Aggregate sales by customer_code
            sales_q = self.db.execute(
                select(
                    SalesHistory.customer_code,
                    func.sum(func.coalesce(SalesHistory.amount, SalesHistory.qty_sold * func.coalesce(SalesHistory.rate, 1.0))),
                    func.max(SalesHistory.sale_date),
                    func.count(SalesHistory.id)
                )
                .where(SalesHistory.customer_code.in_(c_codes))
                .group_by(SalesHistory.customer_code)
            ).all()
            sales_map = {row[0]: (row[1] or 0.0, row[2], row[3] or 0) for row in sales_q}

            # Aggregate receivables by customer_code
            dues_q = self.db.execute(
                select(
                    CustomerReceivable.customer_code,
                    func.sum(CustomerReceivable.outstanding_amount),
                    func.count(CustomerReceivable.id),
                    func.max(CustomerReceivable.days_due),
                    func.sum(CustomerReceivable.adjusted_amount)
                )
                .where(CustomerReceivable.customer_code.in_(c_codes))
                .group_by(CustomerReceivable.customer_code)
            ).all()
            dues_map = {row[0]: (row[1] or 0.0, row[2] or 0, row[3] or 0, row[4] or 0.0) for row in dues_q}

            results = []
            for c in customers:
                s_tot, s_max_dt, s_cnt = sales_map.get(c.customer_code, (0.0, None, 0))
                d_tot, d_cnt, d_max_days, cr_tot = dues_map.get(c.customer_code, (0.0, 0, 0, 0.0))

                results.append({
                    'customer_code': c.customer_code,
                    'customer_name': c.customer_name,
                    'group_name': getattr(c, 'group_name', None) or 'General',
                    'district': c.district or 'General',
                    'salesperson': c.salesperson or 'Unassigned',
                    'credit_limit': c.credit_limit or 0.0,
                    'status': c.status or 'ACTIVE',
                    'total_sales': round(s_tot, 2),
                    'current_dues': round(d_tot, 2),
                    'company_payable': round(cr_tot, 2),
                    'credit_amount': round(cr_tot, 2),
                    'net_receivable': round(d_tot - cr_tot, 2),
                    'total_orders': s_cnt,
                    'last_order_date': s_max_dt.strftime('%d-%b-%Y') if s_max_dt else 'No Orders',
                    'oldest_due_days': d_max_days,
                })
            if zero_sales_dues_gt_zero:
                results = [c for c in results if c.get('total_sales', 0.0) == 0.0 and c.get('current_dues', 0.0) > 0.0]
            return results

        # Fallback: synthesize customer records from sales_history if customers table is empty
        synth_sales = self.db.execute(
            select(
                SalesHistory.customer_code,
                SalesHistory.customer_name,
                func.sum(func.coalesce(SalesHistory.amount, SalesHistory.qty_sold * func.coalesce(SalesHistory.rate, 1.0))),
                func.max(SalesHistory.sale_date),
                func.count(SalesHistory.id)
            )
            .where(SalesHistory.customer_code.is_not(None))
            .group_by(SalesHistory.customer_code, SalesHistory.customer_name)
            .limit(limit)
        ).all()

        results = []
        for row in synth_sales:
            code, name, tot, max_dt, cnt = row[0], row[1] or row[0], row[2] or 0.0, row[3], row[4] or 0
            if search and (search.lower() not in code.lower() and search.lower() not in name.lower()):
                continue
            results.append({
                'customer_code': code,
                'customer_name': name,
                'group_name': 'General',
                'district': 'West Bengal',
                'salesperson': 'Sales Team',
                'credit_limit': 100000.0,
                'status': 'ACTIVE',
                'total_sales': round(tot, 2),
                'current_dues': 0.0,
                'company_payable': 0.0,
                'credit_amount': 0.0,
                'net_receivable': 0.0,
                'total_orders': cnt,
                'last_order_date': max_dt.strftime('%d-%b-%Y') if max_dt else 'No Orders',
                'oldest_due_days': 0,
            })
        if zero_sales_dues_gt_zero:
            results = [c for c in results if c.get('total_sales', 0.0) == 0.0 and c.get('current_dues', 0.0) > 0.0]
        return sorted(results, key=lambda x: x['customer_name'].upper())

    def get_sales_dashboard_overview(self, group: Optional[str] = None) -> dict[str, Any]:
        """
        Computes executive sales dashboard overview KPIs and customer category bins by sales volume:
        - total_customers
        - total_sales
        - active_customers (customers with total_sales > 0)
        - total_dues (Debit dues from Master Ledger)
        - total_payable (Credit liabilities from Master Ledger)
        - net_receivable
        - total_orders
        - zero_sales_dues_count: customers with total_sales == 0 and current_dues > 0
        - zero_sales_dues_amount: aggregate outstanding dues for zero-sales customers
        - zero_sales_dues_codes: list of customer codes with zero sales and dues > 0
        - customer_sales_bins:
            - '0 to 2 Lakh': 0 <= sales <= 200,000
            - '>2 to 5 Lakh': 200,000 < sales <= 500,000
            - '>5 to 8 Lakh': 500,000 < sales <= 800,000
            - '>8 to 12 Lakh': 800,000 < sales <= 1,200,000
            - '>12 to 20 Lakh': 1,200,000 < sales <= 2,000,000
            - '>20 Lakh': sales > 2,000,000
        """
        custs = self.list_customers(group=group, limit=10000)
        tot_customers = len(custs)
        tot_sales = sum(c.get('total_sales', 0.0) for c in custs)
        tot_dues = sum(c.get('current_dues', 0.0) for c in custs)
        tot_payable = sum(c.get('company_payable', c.get('credit_amount', 0.0)) for c in custs)
        tot_orders = sum(c.get('total_orders', 0) for c in custs)
        active_customers = sum(1 for c in custs if c.get('total_sales', 0.0) > 0)

        # Zero sales with dues > 0 analysis
        zero_sales_dues_custs = [c for c in custs if c.get('total_sales', 0.0) == 0.0 and c.get('current_dues', 0.0) > 0.0]
        zero_sales_dues_count = len(zero_sales_dues_custs)
        zero_sales_dues_amount = sum(c.get('current_dues', 0.0) for c in zero_sales_dues_custs)

        bin_specs = [
            {'bin_id': '0_to_2L', 'bin_label': '0 to 2 Lakh', 'min_val': 0.0, 'max_val': 200000.0},
            {'bin_id': '2_to_5L', 'bin_label': '>2 to 5 Lakh', 'min_val': 200000.0, 'max_val': 500000.0},
            {'bin_id': '5_to_8L', 'bin_label': '>5 to 8 Lakh', 'min_val': 500000.0, 'max_val': 800000.0},
            {'bin_id': '8_to_12L', 'bin_label': '>8 to 12 Lakh', 'min_val': 800000.0, 'max_val': 1200000.0},
            {'bin_id': '12_to_20L', 'bin_label': '>12 to 20 Lakh', 'min_val': 1200000.0, 'max_val': 2000000.0},
            {'bin_id': 'above_20L', 'bin_label': '>20 Lakh', 'min_val': 2000000.0, 'max_val': float('inf')},
        ]

        bins_result = []
        for spec in bin_specs:
            min_v = spec['min_val']
            max_v = spec['max_val']
            if max_v == float('inf'):
                in_bin = [c for c in custs if c.get('total_sales', 0.0) > min_v]
            elif min_v == 0.0:
                in_bin = [c for c in custs if 0.0 <= c.get('total_sales', 0.0) <= max_v]
            else:
                in_bin = [c for c in custs if min_v < c.get('total_sales', 0.0) <= max_v]

            cnt = len(in_bin)
            bin_sales = sum(c.get('total_sales', 0.0) for c in in_bin)
            c_pct = round((cnt / tot_customers * 100.0), 2) if tot_customers > 0 else 0.0
            s_pct = round((bin_sales / tot_sales * 100.0), 2) if tot_sales > 0 else 0.0

            bins_result.append({
                'bin_id': spec['bin_id'],
                'bin_label': spec['bin_label'],
                'min_value': min_v,
                'max_value': None if max_v == float('inf') else max_v,
                'customer_count': cnt,
                'customer_percentage': c_pct,
                'total_sales': round(bin_sales, 2),
                'sales_percentage': s_pct,
                'customer_codes': [c['customer_code'] for c in in_bin],
            })

        return {
            'total_customers': tot_customers,
            'active_customers': active_customers,
            'inactive_customers': tot_customers - active_customers,
            'total_sales': round(tot_sales, 2),
            'total_dues': round(tot_dues, 2),
            'total_payable': round(tot_payable, 2),
            'net_receivable': round(tot_dues - tot_payable, 2),
            'total_orders': tot_orders,
            'zero_sales_dues_count': zero_sales_dues_count,
            'zero_sales_dues_amount': round(zero_sales_dues_amount, 2),
            'zero_sales_dues_codes': [c['customer_code'] for c in zero_sales_dues_custs],
            'customer_sales_bins': bins_result,
        }

    def _get_customer_sales(self, customer_code: str, cust_name: str) -> list[SalesHistory]:

        """
        Retrieves all sales records for a customer, matching by:
        - Customer Code
        - Customer Name (case-insensitive)
        - Canonical name/code variations
        """
        matching_codes = {customer_code}
        matching_names = {cust_name.strip().upper()}
        c_norm = normalize_customer_name(cust_name)
        if c_norm:
            matching_names.add(c_norm.upper())
        c_key = customer_canonical_key(cust_name)
        if c_key:
            matching_codes.add(f"CUST-{c_key[:40]}")
            matching_codes.add(f"CUST-{c_key[:25]}")
            matching_codes.add(f"CUST-{c_key}")

        sales_records = self.db.scalars(
            select(SalesHistory)
            .where(
                (SalesHistory.customer_code.in_(matching_codes)) |
                (func.upper(SalesHistory.customer_name).in_(matching_names))
            )
            .order_by(desc(SalesHistory.sale_date))
        ).all()

        if not sales_records and c_key:
            # Fallback scan in case of legacy records
            all_sales = self.db.scalars(select(SalesHistory)).all()
            for s in all_sales:
                s_name = s.customer_name or ''
                s_code = (s.customer_code or '').replace('CUST-', '')
                if customer_canonical_key(s_name) == c_key or customer_canonical_key(s_code) == c_key:
                    sales_records.append(s)

        return sales_records

    def get_customer_summary(self, customer_code: str) -> dict[str, Any]:
        """
        Computes detailed financial and operational summary for a single customer:
        - Total Historical Sales
        - Sales Last 30 Days & 90 Days
        - Outstanding Dues & Bill-wise Ageing
        - Oldest Due & Days
        - Purchase History Lines
        - Pending Invoices
        """
        now = datetime.utcnow()
        t30 = now - timedelta(days=30)
        t90 = now - timedelta(days=90)

        # 1. Fetch Customer Record
        customer = self.db.scalars(
            select(Customer).where(Customer.customer_code == customer_code)
        ).first()

        cust_name = customer.customer_name if customer else customer_code
        district = customer.district if customer else 'General'
        salesperson = customer.salesperson if customer else 'Unassigned'
        credit_limit = customer.credit_limit if customer else 0.0

        # 2. Sales Transactions for this customer
        sales_records = self._get_customer_sales(customer_code, cust_name)

        prod_catalog = {
            p.product_code: p for p in self.db.scalars(select(Product)).all()
        }

        total_sales = 0.0
        sales_30d = 0.0
        sales_90d = 0.0
        invoices_set = set()
        last_order_dt = None

        recent_orders = []
        prod_agg: dict[str, dict[str, Any]] = {}

        for s in sales_records:
            amt = s.amount if (s.amount and s.amount > 0) else (s.qty_sold * (s.rate or 0.0))
            if amt <= 0 and s.qty_sold > 0:
                amt = s.qty_sold * 10.0  # Heuristic fallback if rate is missing
            total_sales += amt

            if s.sale_date:
                if s.sale_date >= t30:
                    sales_30d += amt
                if s.sale_date >= t90:
                    sales_90d += amt
                if not last_order_dt or s.sale_date > last_order_dt:
                    last_order_dt = s.sale_date

            inv_ref = s.invoice_no or f"INV-{s.id}"
            invoices_set.add(inv_ref)

            free_val = getattr(s, 'free_qty', 0.0) or 0.0

            recent_orders.append({
                'invoice_no': inv_ref,
                'date': s.sale_date.strftime('%d-%b-%Y') if s.sale_date else 'N/A',
                'product_code': s.product_code,
                'product_name': s.product_name or s.product_code,
                'description': s.product_name or s.product_code,
                'batch_no': s.batch_no or 'DEFAULT',
                'qty': s.qty_sold,
                'free': free_val,
                'rate': s.rate or 0.0,
                'amount': round(amt, 2),
            })

            # Aggregate into Transacted Products Portfolio
            p_code = s.product_code or 'UNKNOWN'
            p_name = (s.product_name or p_code).strip()
            if p_code not in prod_agg:
                p_cat_obj = prod_catalog.get(p_code)
                prod_agg[p_code] = {
                    'product_code': p_code,
                    'product_name': p_name,
                    'description': p_name,
                    'company': getattr(p_cat_obj, 'company', None) or getattr(s, 'channel', None) or 'General',
                    'category': getattr(p_cat_obj, 'category', None) or 'Tablets',
                    'total_qty': 0.0,
                    'total_free': 0.0,
                    'total_amount': 0.0,
                    'order_count': 0,
                    'rates': [],
                    'last_purchase_date': None,
                    'last_purchase_date_str': 'N/A',
                }
            entry = prod_agg[p_code]
            if p_name and (not entry['product_name'] or entry['product_name'] == p_code):
                entry['product_name'] = p_name
                entry['description'] = p_name
            entry['total_qty'] += s.qty_sold
            entry['total_free'] += free_val
            entry['total_amount'] += amt
            entry['order_count'] += 1
            if s.rate and s.rate > 0:
                entry['rates'].append(s.rate)
            if s.sale_date:
                if not entry['last_purchase_date'] or s.sale_date > entry['last_purchase_date']:
                    entry['last_purchase_date'] = s.sale_date
                    entry['last_purchase_date_str'] = s.sale_date.strftime('%d-%b-%Y')

        transacted_products = []
        for p_code, p_info in prod_agg.items():
            rates = p_info['rates']
            avg_rate = round(sum(rates) / len(rates), 2) if rates else (
                round(p_info['total_amount'] / p_info['total_qty'], 2) if p_info['total_qty'] > 0 else 0.0
            )
            transacted_products.append({
                'product_code': p_info['product_code'],
                'product_name': p_info['product_name'],
                'description': p_info['description'],
                'company': p_info['company'],
                'category': p_info['category'],
                'qty': round(p_info['total_qty'], 2),
                'total_qty': round(p_info['total_qty'], 2),
                'free': round(p_info['total_free'], 2),
                'total_free': round(p_info['total_free'], 2),
                'rate': avg_rate,
                'avg_rate': avg_rate,
                'amount': round(p_info['total_amount'], 2),
                'total_amount': round(p_info['total_amount'], 2),
                'order_count': p_info['order_count'],
                'last_purchase_date': p_info['last_purchase_date_str'],
            })
        transacted_products.sort(key=lambda x: x['total_amount'], reverse=True)

        product_totals = {
            'total_qty': round(sum(p['qty'] for p in transacted_products), 2),
            'total_free': round(sum(p['free'] for p in transacted_products), 2),
            'total_amount': round(sum(p['amount'] for p in transacted_products), 2),
        }

        total_orders = len(invoices_set) if invoices_set else len(sales_records)
        days_since_last_order = (now - last_order_dt).days if last_order_dt else None

        # 3. Customer Receivables / Outstanding
        receivables = self.db.scalars(
            select(CustomerReceivable)
            .where(CustomerReceivable.customer_code == customer_code)
            .order_by(desc(CustomerReceivable.days_due))
        ).all()

        total_outstanding = 0.0
        total_payable = 0.0
        oldest_due_days = 0
        oldest_due_invoice = None

        ageing = {
            'current': 0.0,
            '1_30': 0.0,
            '31_60': 0.0,
            '61_90': 0.0,
            'gt_90': 0.0,
        }

        pending_invoices = []
        for r in receivables:
            bal = r.outstanding_amount or 0.0
            payable = r.adjusted_amount or 0.0
            total_payable += payable
            if bal <= 0 and payable <= 0:
                continue
            total_outstanding += bal
            days = r.days_due or 0
            if bal > 0 and days > oldest_due_days:
                oldest_due_days = days
                oldest_due_invoice = r.invoice_no

            # Ageing categorization (for customer dues)
            if days <= 0:
                ageing['current'] += bal
            elif days <= 30:
                ageing['1_30'] += bal
            elif days <= 60:
                ageing['31_60'] += bal
            elif days <= 90:
                ageing['61_90'] += bal
            else:
                ageing['gt_90'] += bal

            pending_invoices.append({
                'invoice_no': r.invoice_no,
                'invoice_date': r.invoice_date.strftime('%d-%b-%Y') if r.invoice_date else 'N/A',
                'due_date': r.due_date.strftime('%d-%b-%Y') if r.due_date else 'N/A',
                'invoice_amount': round(r.invoice_amount or 0.0, 2),
                'outstanding_amount': round(bal, 2),
                'company_payable': round(payable, 2),
                'credit_amount': round(payable, 2),
                'days_due': days,
                'ageing_bucket': r.ageing_bucket or ('Overdue' if days > 0 else 'Current'),
            })

        avg_order_val = (total_sales / total_orders) if total_orders > 0 else 0.0

        return {
            'customer_code': customer_code,
            'customer_name': cust_name,
            'group_name': getattr(customer, 'group_name', None) or 'General',
            'district': district,
            'salesperson': salesperson,
            'credit_limit': credit_limit,
            'total_sales': round(total_sales, 2),
            'sales_30d': round(sales_30d, 2),
            'sales_90d': round(sales_90d, 2),
            'total_orders': total_orders,
            'average_order_value': round(avg_order_val, 2),
            'current_outstanding': round(total_outstanding, 2),
            'current_dues': round(total_outstanding, 2),
            'company_payable': round(total_payable, 2),
            'credit_amount': round(total_payable, 2),
            'net_receivable': round(total_outstanding - total_payable, 2),
            'outstanding_invoices_count': len(pending_invoices),
            'oldest_due_invoice': oldest_due_invoice or 'None',
            'oldest_due_days': oldest_due_days,
            'last_order_date': last_order_dt.strftime('%d-%b-%Y') if last_order_dt else 'No Orders',
            'days_since_last_order': days_since_last_order if days_since_last_order is not None else 'N/A',
            'ageing': {k: round(v, 2) for k, v in ageing.items()},
            'pending_invoices': pending_invoices,
            'product_totals': product_totals,
            'transacted_products': transacted_products,
            'recent_orders': recent_orders[:500],
        }

    def get_customer_recommendations(self, customer_code: str) -> dict[str, list[dict[str, Any]]]:
        """
        Computes tailored product recommendations for the selected customer:
        1. Near-Expiry Opportunities (Expiry <= configured horizon days, default 180, for products this customer buys)
        2. Regular Reorder Candidates (Frequently bought items due for replenishment)
        """
        now = datetime.utcnow()
        horizon_days = int(getattr(settings, 'expiry_risk_horizon_days', 180)) if settings else 180
        horizon_dt = now + timedelta(days=horizon_days)

        # 1. Find all products historically purchased by this customer
        customer = self.db.scalars(
            select(Customer).where(Customer.customer_code == customer_code)
        ).first()
        cust_name = customer.customer_name if customer else customer_code

        sales = self._get_customer_sales(customer_code, cust_name)

        prod_history = defaultdict(list)
        for s in sales:
            k = (s.product_code, (s.product_name or s.product_code).strip().upper())
            prod_history[k].append(s)

        # 2. Pre-fetch inventory batches & products
        batches = self.db.scalars(
            select(InventoryBatch).where(InventoryBatch.qty_on_hand > 0)
        ).all()
        batch_map = defaultdict(list)
        for b in batches:
            if getattr(b, 'is_promo_material', False):
                continue
            batch_map[b.product_code].append(b)

        near_expiry_opps = []
        regular_reorders = []

        for (p_code, p_name), orders in prod_history.items():
            order_count = len(orders)
            total_qty = sum(o.qty_sold for o in orders)
            typical_qty = round(total_qty / max(1, order_count), 1)

            # Sort orders by date
            orders_with_date = [o for o in orders if o.sale_date]
            orders_with_date.sort(key=lambda o: o.sale_date, reverse=True)
            last_order = orders_with_date[0] if orders_with_date else None
            last_order_dt = last_order.sale_date if last_order else None
            days_ago = (now - last_order_dt).days if last_order_dt else 30

            # Calculate reorder interval
            reorder_interval = 30
            if len(orders_with_date) >= 2:
                intervals = []
                for i in range(len(orders_with_date) - 1):
                    diff = (orders_with_date[i].sale_date - orders_with_date[i + 1].sale_date).days
                    if diff > 0:
                        intervals.append(diff)
                if intervals:
                    reorder_interval = int(sum(intervals) / len(intervals))

            avail_batches = batch_map.get(p_code, [])

            # Check Near-Expiry Opportunities
            for b in avail_batches:
                if b.expiry_date and now < b.expiry_date <= horizon_dt:
                    days_remaining = (b.expiry_date - now).days
                    suggested_qty = min(typical_qty, b.qty_on_hand)
                    if suggested_qty <= 0:
                        continue

                    # Confidence
                    confidence = 'HIGH' if order_count >= 3 else 'MEDIUM'
                    is_critical = days_remaining <= 30

                    reason = (
                        f"Customer has purchased this product {order_count} times. "
                        f"Current batch expires in {days_remaining} days. "
                        f"Customer typically purchases approximately {suggested_qty:g} units."
                    )

                    near_expiry_opps.append({
                        'product_code': p_code,
                        'product_name': p_name,
                        'company': b.company or 'General',
                        'batch_no': b.batch_no or 'DEFAULT',
                        'expiry_date': b.expiry_date.strftime('%d-%b-%Y'),
                        'days_remaining': days_remaining,
                        'is_critical': is_critical,
                        'current_stock': b.qty_on_hand,
                        'customer_typical_qty': typical_qty,
                        'last_purchase': f"{days_ago} days ago" if last_order_dt else 'N/A',
                        'suggested_qty': suggested_qty,
                        'confidence': confidence,
                        'reason': reason,
                    })

            # Check Regular Reorder Candidates
            total_stock = sum(b.qty_on_hand for b in avail_batches)
            if total_stock > 0 and days_ago >= (reorder_interval * 0.8):
                regular_reorders.append({
                    'product_code': p_code,
                    'product_name': p_name,
                    'company': avail_batches[0].company if avail_batches else 'General',
                    'order_count': order_count,
                    'typical_qty': typical_qty,
                    'suggested_qty': typical_qty,
                    'last_purchase': f"{days_ago} days ago",
                    'reorder_interval': f"{reorder_interval} days",
                    'available_stock': total_stock,
                    'reason': f"Regular purchase ({order_count} orders). Expected reorder cycle ({reorder_interval}d) reached. Available stock: {total_stock:g}.",
                    'priority': 'HIGH' if days_ago >= reorder_interval else 'MEDIUM',
                })

        # Sort recommendations
        near_expiry_opps.sort(key=lambda x: (x['days_remaining'], -x['suggested_qty']))
        regular_reorders.sort(key=lambda x: (-x['order_count'], -x['typical_qty']))

        return {
            'near_expiry_opportunities': near_expiry_opps,
            'regular_reorders': regular_reorders,
        }

    @staticmethod
    def seed_demo_data(db: Session) -> dict:
        """Seeds realistic demo customers, receivables, and sales history for instant evaluation."""
        now = datetime.utcnow()

        demo_customers = [
            {
                'customer_code': 'CUST-001',
                'customer_name': 'ABC MEDICAL HALL',
                'address': '12 B.T. Road, Shyambazar',
                'area': 'North Kolkata',
                'district': 'Kolkata',
                'salesperson': 'A. Sengupta',
                'credit_limit': 150000.0,
            },
            {
                'customer_code': 'CUST-002',
                'customer_name': 'DWARKA PHARMA DISTRIBUTORS',
                'address': '45 G.T. Road, Howrah Maidan',
                'area': 'Howrah Station',
                'district': 'Howrah',
                'salesperson': 'R. Banerjee',
                'credit_limit': 300000.0,
            },
            {
                'customer_code': 'CUST-003',
                'customer_name': 'MAA TARA PHARMACY',
                'address': 'Station Road, Burdwan',
                'area': 'Burdwan Sadar',
                'district': 'Purba Bardhaman',
                'salesperson': 'S. Mukherjee',
                'credit_limit': 80000.0,
            },
            {
                'customer_code': 'CUST-004',
                'customer_name': 'BENGAL MEDICARE DISTRIBUTORS',
                'address': 'Kharagpur Town',
                'area': 'Golbazar',
                'district': 'Paschim Medinipur',
                'salesperson': 'K. Ghosh',
                'credit_limit': 250000.0,
            },
            {
                'customer_code': 'CUST-005',
                'customer_name': 'HEALTHPLUS MEDICAL STORE',
                'address': 'Siliguri Hill Cart Road',
                'area': 'Sevoke Road',
                'district': 'Darjeeling',
                'salesperson': 'P. Roy',
                'credit_limit': 120000.0,
            },
        ]

        for c_data in demo_customers:
            existing = db.scalars(select(Customer).where(Customer.customer_code == c_data['customer_code'])).first()
            if not existing:
                db.add(Customer(**c_data))

        db.flush()

        # Add sample receivables
        demo_receivables = [
            ('CUST-001', 'ABC MEDICAL HALL', 'INV-2026-881', now - timedelta(days=67), now - timedelta(days=37), 25000.0, 25000.0, 67, '61–90 Days'),
            ('CUST-001', 'ABC MEDICAL HALL', 'INV-2026-942', now - timedelta(days=25), now + timedelta(days=5), 17500.0, 17500.0, 0, 'Current'),
            ('CUST-002', 'DWARKA PHARMA DISTRIBUTORS', 'INV-2026-720', now - timedelta(days=110), now - timedelta(days=80), 60000.0, 60000.0, 110, '> 90 Days'),
            ('CUST-002', 'DWARKA PHARMA DISTRIBUTORS', 'INV-2026-890', now - timedelta(days=45), now - timedelta(days=15), 35000.0, 35000.0, 45, '31–60 Days'),
            ('CUST-003', 'MAA TARA PHARMACY', 'INV-2026-955', now - timedelta(days=18), now + timedelta(days=12), 18200.0, 18200.0, 0, 'Current'),
        ]

        for c_code, c_name, inv, idt, ddt, iamt, oamt, ddue, abuck in demo_receivables:
            ex = db.scalars(select(CustomerReceivable).where(CustomerReceivable.invoice_no == inv)).first()
            if not ex:
                db.add(CustomerReceivable(
                    customer_code=c_code,
                    customer_name=c_name,
                    invoice_no=inv,
                    invoice_date=idt,
                    due_date=ddt,
                    invoice_amount=iamt,
                    outstanding_amount=oamt,
                    days_due=ddue,
                    ageing_bucket=abuck,
                ))

        # Add sample sales history lines
        demo_sales = [
            ('CUST-001', 'ABC MEDICAL HALL', 'A00154', 'B-ROXIN E/D DROP 1X10ML', 'INV-2026-942', now - timedelta(days=25), 10.0, 7.0, 70.0),
            ('CUST-001', 'ABC MEDICAL HALL', 'MED-BPMOX', 'BP-MOX 625 TABLET', 'INV-2026-942', now - timedelta(days=25), 20.0, 120.0, 2400.0),
            ('CUST-001', 'ABC MEDICAL HALL', 'MED-BPMOX', 'BP-MOX 625 TABLET', 'INV-2026-881', now - timedelta(days=55), 15.0, 120.0, 1800.0),
            ('CUST-001', 'ABC MEDICAL HALL', 'MED-BPMOX', 'BP-MOX 625 TABLET', 'INV-2026-810', now - timedelta(days=82), 20.0, 120.0, 2400.0),
            ('CUST-001', 'ABC MEDICAL HALL', 'MED-PCM650', 'PARACET-650 MG', 'INV-2026-942', now - timedelta(days=25), 50.0, 22.0, 1100.0),
            ('CUST-002', 'DWARKA PHARMA DISTRIBUTORS', 'MED-AZI500', 'AZITHRO-500 TAB', 'INV-2026-890', now - timedelta(days=45), 100.0, 85.0, 8500.0),
            ('CUST-002', 'DWARKA PHARMA DISTRIBUTORS', 'MED-PAN40', 'PANTOP-40 TAB', 'INV-2026-720', now - timedelta(days=110), 120.0, 48.0, 5760.0),
            ('CUST-003', 'MAA TARA PHARMACY', 'MED-BPMOX', 'BP-MOX 625 TABLET', 'INV-2026-955', now - timedelta(days=18), 30.0, 120.0, 3600.0),
        ]

        for c_code, c_name, p_code, p_name, inv, sdt, qty, rate, amt in demo_sales:
            db.add(SalesHistory(
                customer_code=c_code,
                customer_name=c_name,
                product_code=p_code,
                product_name=p_name,
                invoice_no=inv,
                sale_date=sdt,
                qty_sold=qty,
                rate=rate,
                amount=amt,
            ))

        # Add Near-expiry inventory batches for BP-MOX 625 and B-ROXIN if missing
        batches = [
            ('MED-BPMOX', 'B-123', now + timedelta(days=55), 50.0, 95.0, 'CIPLA LTD'),
            ('A00154', 'B-ROX-99', now + timedelta(days=28), 40.0, 5.5, 'BIO-ROX LABS'),
        ]
        for pcode, bno, exp, qty, cost, comp in batches:
            ex_b = db.scalars(select(InventoryBatch).where(
                InventoryBatch.product_code == pcode,
                InventoryBatch.batch_no == bno
            )).first()
            if not ex_b:
                db.add(InventoryBatch(
                    product_code=pcode,
                    batch_no=bno,
                    expiry_date=exp,
                    qty_on_hand=qty,
                    unit_cost=cost,
                    company=comp,
                ))

        db.commit()

        return {
            'customers': len(demo_customers),
            'receivables': len(demo_receivables),
            'sales_lines': len(demo_sales),
        }
