"""
MARG Excel Ingestion Service

Takes parsed data structures from MargExcelParser and commits them to the database.
Handles database purge on fresh uploads, and upserts Products, Suppliers,
InventoryBatches, and SalesHistory with foreign key integrity.

Key deduplication guarantee:
  If the same product_name already exists in the DB under a different product_code
  (e.g. stock report used real MARG code 'A00920', sales report generated 'MED-82877'),
  the incoming data is remapped to the EXISTING code — no duplicate product is created.
"""
import re
from typing import Any
from sqlalchemy.orm import Session
from sqlalchemy import delete, select, func, update
from backend.app.models.entities import (
    Product, Supplier, InventoryBatch, SalesHistory,
    ProcurementRun, ProcurementProposal, FeedbackEvent,
    Customer, CustomerReceivable
)
from backend.app.services.audit import audit
from backend.app.adapters.excel import (
    MargExcelParser, normalize_customer_name, customer_canonical_key, generate_customer_code
)


class CustomerResolver:
    """
    Bidirectional customer resolver across Master Customer Ledger and Product-Wise Customer Ledger.
    Normalizes customer names and guarantees that both Excels link to the exact same Customer record
    and customer_code, so that Customer Dues (Debit), Company Liable to Pay (Credit), and Product
    Sales History are unified under the exact same customer.
    """

    def __init__(self, db: Session):
        self.db = db
        self.code_map: dict[str, Customer] = {}
        self.canonical_map: dict[str, Customer] = {}
        self.norm_name_map: dict[str, Customer] = {}
        self.slug25_map: dict[str, Customer] = {}
        self._refresh()

    def _refresh(self):
        custs = self.db.scalars(select(Customer)).all()
        self.code_map = {c.customer_code: c for c in custs if c.customer_code}
        self.canonical_map = {customer_canonical_key(c.customer_name): c for c in custs if c.customer_name}
        self.norm_name_map = {normalize_customer_name(c.customer_name): c for c in custs if c.customer_name}
        self.slug25_map = {customer_canonical_key(c.customer_name)[:25]: c for c in custs if c.customer_name}

    def register_customer(self, cust: Customer):
        """Registers an added or updated customer in the lookup maps."""
        if cust.customer_code:
            self.code_map[cust.customer_code] = cust
        if cust.customer_name:
            ck = customer_canonical_key(cust.customer_name)
            self.canonical_map[ck] = cust
            self.norm_name_map[normalize_customer_name(cust.customer_name)] = cust
            if len(ck) >= 25:
                self.slug25_map[ck[:25]] = cust

    def resolve(self, raw_name: str | None, raw_code: str | None = None) -> tuple[str, str]:
        """
        Resolves customer name or code to (canonical_customer_code, canonical_customer_name).
        Matching hierarchy:
        1. Exact Customer Code in DB
        2. Exact Canonical Key match (punctuation & space-insensitive, e.g. ABMEDICINECENTRETHIRDPARTPURBABURDWAN)
        3. Normalized Name match
        4. 25-character slug prefix match (for backwards-compatibility with legacy slugs)
        5. Substring / containment match for party names (len >= 12)
        6. Default: deterministic generate_customer_code(norm_name)
        """
        if not raw_name and not raw_code:
            return "CUST-GENERAL", "General Customer"

        c_name = normalize_customer_name(raw_name or "")
        c_canon = customer_canonical_key(raw_name or "")

        # 1. Exact Customer Code
        if raw_code and raw_code in self.code_map:
            c = self.code_map[raw_code]
            return c.customer_code, c.customer_name

        # 2. Canonical Key Match (e.g. ABMEDICINECENTRETHIRDPARTPURBABURDWAN)
        if c_canon and c_canon in self.canonical_map:
            c = self.canonical_map[c_canon]
            return c.customer_code, c.customer_name

        # 3. Normalized Name Match
        if c_name and c_name in self.norm_name_map:
            c = self.norm_name_map[c_name]
            return c.customer_code, c.customer_name

        # 4. 25-char slug match
        if c_canon and len(c_canon) >= 25 and c_canon[:25] in self.slug25_map:
            c = self.slug25_map[c_canon[:25]]
            return c.customer_code, c.customer_name

        # 5. Substring match for names with length >= 12
        if c_canon and len(c_canon) >= 12:
            for ex_canon, c in self.canonical_map.items():
                if len(ex_canon) >= 12 and (c_canon in ex_canon or ex_canon in c_canon):
                    return c.customer_code, c.customer_name

        # 6. Fallback: generate deterministic code and normalized name
        det_code = generate_customer_code(c_name) if c_name else (raw_code or "CUST-UNKNOWN")
        det_name = c_name if c_name else (raw_code or "Unknown Customer")
        return det_code, det_name


class IngestionService:
    """
    Transactional database ingestion service for MARG ERP data.

    Deduplication guarantee:
      Same product name → same product code, always.
      When stock report uses real MARG code (e.g. A00920) and sales report
      generates a hash code (e.g. MED-82877) for the same product name,
      both are merged into the existing canonical code. No duplicates.
    """

    def __init__(self, db: Session) -> None:
        self.db: Session = db
        # Cache: product_name (upper-stripped) → existing product_code in DB
        self._name_to_code: dict[str, str] = {}
        self._existing_codes: set[str] = set()
        self._existing_code_batches: set[tuple[str, str]] = set()

    def _build_name_index(self) -> None:
        """Load all existing product names → codes into the local dedup cache."""
        from backend.app.adapters.excel import pharma_canonical_key, canonical_medicine_key, _make_stable_code
        rows = self.db.execute(
            select(Product.product_code, Product.product_name, Product.category, Product.batch_no)
        ).all()
        self._existing_codes = {r[0] for r in rows if r[0]}
        self._existing_code_batches = {(r[0], r[3]) for r in rows if r[0] and r[3]}

        sorted_rows = sorted(rows, key=lambda r: 1 if (r.category or '').startswith('ZZZZ') else 0)
        self._name_to_code = {}
        for row in sorted_rows:
            pname = (row.product_name or '').strip()
            if not pname:
                continue
            raw_key = pname.upper()
            if raw_key not in self._name_to_code:
                self._name_to_code[raw_key] = row.product_code
            c_key = canonical_medicine_key(pname)
            if c_key and c_key not in self._name_to_code:
                self._name_to_code[c_key] = row.product_code
            pkey = pharma_canonical_key(pname)
            if pkey and pkey not in self._name_to_code:
                self._name_to_code[pkey] = row.product_code
            s_code = _make_stable_code(pname)
            if s_code and s_code not in self._name_to_code:
                self._name_to_code[s_code] = row.product_code

    def _resolve_code(self, incoming_code: str, incoming_name: str) -> str:
        """
        Returns the canonical product_code to use for this product in O(1) time without DB roundtrips.

        Priority:
          1. Exact code already in DB          → use it.
          2. Same product_name in DB           → reuse existing code (deduplication).
          3. Canonical medicine key match      → reuse existing code (MARG column cuts/spacing).
          4. Pharma formulation match          → reuse existing code (packaging format variation).
          5. Neither                           → incoming_code is new, use it.
        """
        if incoming_code and incoming_code in self._existing_codes:
            return incoming_code
        name_key = (incoming_name or '').strip().upper()
        if name_key and name_key in self._name_to_code:
            return self._name_to_code[name_key]
        from backend.app.adapters.excel import pharma_canonical_key, canonical_medicine_key, _make_stable_code
        c_key = canonical_medicine_key(incoming_name)
        if c_key and c_key in self._name_to_code:
            return self._name_to_code[c_key]
        pkey = pharma_canonical_key(incoming_name)
        if pkey and pkey in self._name_to_code:
            return self._name_to_code[pkey]
        if incoming_code and incoming_code in self._name_to_code:
            return self._name_to_code[incoming_code]
        if incoming_name:
            stable = _make_stable_code(incoming_name)
            if stable in self._name_to_code:
                return self._name_to_code[stable]
            if not incoming_code:
                return stable
        return incoming_code

    def purge_all_data(self) -> dict[str, int]:
        """
        Clears all operational procurement data.
        Ensures a completely fresh start for newly uploaded data.
        """
        del_fb   = self.db.execute(delete(FeedbackEvent)).rowcount
        del_prop = self.db.execute(delete(ProcurementProposal)).rowcount
        del_runs = self.db.execute(delete(ProcurementRun)).rowcount
        del_recv = self.db.execute(delete(CustomerReceivable)).rowcount
        del_cust = self.db.execute(delete(Customer)).rowcount
        del_batch = self.db.execute(delete(InventoryBatch)).rowcount
        del_sales = self.db.execute(delete(SalesHistory)).rowcount
        del_prod = self.db.execute(delete(Product)).rowcount
        del_sup  = self.db.execute(delete(Supplier)).rowcount
        self.db.commit()

        audit(
            self.db,
            event_type='DATABASE_PURGED_FOR_FRESH_IMPORT',
            actor='system-refresh',
            entity_type='database',
            entity_id='all_operational_tables',
            details={
                'deleted_feedback': del_fb,
                'deleted_proposals': del_prop,
                'deleted_runs': del_runs,
                'deleted_receivables': del_recv,
                'deleted_customers': del_cust,
                'deleted_batches': del_batch,
                'deleted_sales': del_sales,
                'deleted_products': del_prod,
                'deleted_suppliers': del_sup,
            },
        )
        self.db.commit()
        return {
            'deleted_products': del_prod,
            'deleted_suppliers': del_sup,
            'deleted_customers': del_cust,
            'deleted_receivables': del_recv,
            'deleted_batches': del_batch,
            'deleted_sales': del_sales,
            'deleted_proposals': del_prop,
        }

    def purge_customer_data(self) -> dict[str, int]:
        """
        Clears all customer master, customer receivables, and customer sales data from the database.
        """
        del_recv = self.db.execute(delete(CustomerReceivable)).rowcount
        del_cust = self.db.execute(delete(Customer)).rowcount
        del_sales = self.db.execute(delete(SalesHistory).where(SalesHistory.customer_code.is_not(None))).rowcount
        self.db.commit()

        audit(
            self.db,
            event_type='CUSTOMER_DATABASE_PURGED',
            actor='user-action',
            entity_type='customers',
            entity_id='customer_tables',
            details={
                'deleted_customers': del_cust,
                'deleted_receivables': del_recv,
                'deleted_sales': del_sales,
            },
        )
        self.db.commit()
        return {
            'deleted_customers': del_cust,
            'deleted_receivables': del_recv,
            'deleted_sales': del_sales,
        }

    def _sync_reorder_from_sales(self) -> int:
        """
        Post-upload sync: for every product that has sales history, recalculate
        its reorder_point based on actual daily demand velocity and update current_stock
        from the sum of its active inventory batches.

        Reorder point formula (pharmaceutical standard):
            reorder_point = avg_daily_demand × lead_time_days × 1.5 (safety factor)
        where lead_time_days defaults to 45 (standard India pharma supply chain).

        Returns the number of products updated.
        """
        from backend.app.services.forecast import DemandService

        LEAD_TIME_DAYS = 45
        SAFETY_FACTOR = 1.5

        # All products that have at least one sales history row
        products_with_sales = self.db.execute(
            select(SalesHistory.product_code).distinct()
        ).scalars().all()

        if not products_with_sales:
            return 0

        demand_svc = DemandService(self.db)

        # Batch stock pre-aggregated in 1 SQL query
        batch_stocks = dict(
            self.db.execute(
                select(InventoryBatch.product_code, func.sum(InventoryBatch.qty_on_hand))
                .group_by(InventoryBatch.product_code)
            ).all()
        )

        updates = []
        for p_code in products_with_sales:
            batch_stock = batch_stocks.get(p_code, 0.0) or 0.0
            avg_daily, _source = demand_svc.forecast_daily(p_code)

            if avg_daily <= 0:
                continue

            new_reorder = round(avg_daily * LEAD_TIME_DAYS * SAFETY_FACTOR, 2)
            updates.append({
                'p_code': p_code,
                'reorder_point': new_reorder,
                'current_stock': batch_stock,
            })

        for u in updates:
            self.db.execute(
                update(Product)
                .where(Product.product_code == u['p_code'])
                .values(
                    reorder_point=u['reorder_point'],
                    current_stock=u['current_stock'],
                )
            )

        self.db.commit()
        return len(updates)

    def ingest_excel(
        self,
        file_content: bytes,
        filename: str = 'marg_export.xlsx',
        clear_existing: bool = False,
    ) -> dict[str, Any]:
        """
        Parses MARG Excel / CSV and upserts products, inventory batches,
        suppliers, and sales history.

        When clear_existing=True, purges all existing records first.

        Deduplication:
          Products with the same name but different codes are merged into
          the already-existing canonical record. All batches and sales rows
          for the incoming (duplicate) code are remapped to the canonical code.

        Post-upload sync:
          After all data is committed, recalculates reorder_point and
          current_stock for every product with sales history so that KPI cards
          and procurement proposals reflect the latest data immediately.
        """
        purged_counts: dict[str, int] = {}
        if clear_existing:
            purged_counts = self.purge_all_data()

        # Build name→code index AFTER any purge so cache is fresh
        self._build_name_index()

        parsed = MargExcelParser.parse_file(file_content, filename=filename)

        products_data  = parsed['products']
        batches_data   = parsed['inventory_batches']
        suppliers_data = parsed['suppliers']
        sales_data     = parsed['sales_history']

        stats = {
            'database_cleared': clear_existing,
            'purged_counts': purged_counts,
            'products_upserted': 0,
            'suppliers_upserted': 0,
            'batches_inserted': 0,
            'sales_inserted': 0,
            'customers_upserted': 0,
            'receivables_upserted': 0,
            'matched_customers': 0,
            'duplicates_merged': 0,
            'reorder_synced': 0,
            'total_rows_parsed': (
                len(products_data) + len(batches_data) +
                len(suppliers_data) + len(sales_data) +
                len(parsed.get('customers', [])) + len(parsed.get('receivables', []))
            ),
        }

        # ── 1. Upsert Suppliers ───────────────────────────────────────────────
        existing_sups = {s.supplier_id: s for s in self.db.scalars(select(Supplier)).all()}
        for s_data in suppliers_data:
            sid = s_data['supplier_id']
            if sid in existing_sups:
                existing = existing_sups[sid]
                for k, v in s_data.items():
                    setattr(existing, k, v)
            else:
                new_sup = Supplier(**s_data)
                self.db.add(new_sup)
                existing_sups[sid] = new_sup
            stats['suppliers_upserted'] += 1

        self.db.flush()

        # ── 2. Insert Products ───────────────────────────────────────────────
        # When ingesting stock status reports, items are preserved with batch & cost.
        # If the file is an auxiliary sales file and product catalog is already populated,
        # skip adding product entries completely to preserve catalog purity.
        is_sales_file = (len(sales_data) > 0 and len(batches_data) == 0)
        has_existing_products = len(self._existing_codes) > 0

        if not (is_sales_file and has_existing_products):
            prods_to_add = []
            for p_data in products_data:
                incoming_code = p_data['product_code']
                incoming_name = p_data['product_name']
                is_stock = p_data.get('is_stock_row', False)

                resolved_code = self._resolve_code(incoming_code, incoming_name)
                is_existing = (resolved_code in self._existing_codes or incoming_code in self._existing_codes)

                if not is_stock:
                    # For auxiliary reports (sales summaries, purchase summaries, outstanding),
                    # never insert duplicate product records if product exists or catalog exists.
                    if is_existing or has_existing_products:
                        stats['duplicates_merged'] += 1
                        continue
                else:
                    # For stock sheets, check if exact batch already exists
                    batch_no = p_data.get('batch_no')
                    if is_existing and batch_no:
                        if (resolved_code, batch_no) in self._existing_code_batches or (incoming_code, batch_no) in self._existing_code_batches:
                            stats['duplicates_merged'] += 1
                            continue

                prod = Product(
                    product_code=resolved_code,
                    product_name=incoming_name,
                    batch_no=p_data.get('batch_no'),
                    expiry_date=p_data.get('expiry_date'),
                    category=p_data.get('category'),
                    company=p_data.get('company'),
                    manufacturer=p_data.get('manufacturer'),
                    is_promo_material=p_data.get('is_promo_material', False),
                    unit=p_data.get('unit', 'units'),
                    pack_size=p_data.get('pack_size', 1.0),
                    min_order_qty=p_data.get('min_order_qty', 1.0),
                    unit_cost=p_data.get('unit_cost', 0.0),
                    current_stock=p_data.get('current_stock', 0.0),
                    reorder_point=p_data.get('reorder_point', 0.0),
                    reorder_enabled=p_data.get('reorder_enabled', True),
                    supplier_name=p_data.get('supplier_name'),
                    preferred_supplier_id=p_data.get('preferred_supplier_id'),
                )
                prods_to_add.append(prod)
                self._existing_codes.add(resolved_code)
                if p_data.get('batch_no'):
                    self._existing_code_batches.add((resolved_code, p_data.get('batch_no')))
                stats['products_upserted'] += 1

            if prods_to_add:
                self.db.add_all(prods_to_add)
                self.db.flush()
                # Refresh cache with any freshly inserted products
                self._build_name_index()

        # ── 3. Replace Inventory Batches ─────────────────────────────────────
        resolved_batches = []
        for b_data in batches_data:
            b_rec = dict(b_data)
            b_code = b_rec['product_code']
            resolved_b_code = self._resolve_code(b_code, b_rec.get('product_name', b_code))
            b_rec['product_code'] = resolved_b_code
            resolved_batches.append(b_rec)

        batch_product_codes = list({b['product_code'] for b in resolved_batches})
        if batch_product_codes:
            missing_codes = [c for c in batch_product_codes if c not in self._existing_codes]
            if missing_codes:
                missing_prods = []
                for p_code in missing_codes:
                    matching_batch = next((b for b in batches_data if b['product_code'] == p_code), None)
                    batch_comp = matching_batch.get('company') if matching_batch else 'General'
                    missing_prods.append(Product(
                        product_code=p_code,
                        product_name=p_code,
                        category='General',
                        company=batch_comp or 'General',
                        unit='unit',
                        pack_size=1.0,
                        unit_cost=0.0,
                    ))
                    self._existing_codes.add(p_code)
                self.db.add_all(missing_prods)
                self.db.flush()

            self.db.execute(
                delete(InventoryBatch).where(
                    InventoryBatch.product_code.in_(batch_product_codes)
                )
            )

        if resolved_batches:
            self.db.add_all([InventoryBatch(**b) for b in resolved_batches])
            stats['batches_inserted'] += len(resolved_batches)

        # ── 4. Upsert Customers ──────────────────────────────────────────────
        # Initialize unified customer resolver
        customer_resolver = CustomerResolver(self.db)

        # ── 4. Upsert Customers ──────────────────────────────────────────────
        customers_data = parsed.get('customers', [])
        if customers_data:
            for c_data in customers_data:
                raw_n = c_data.get('customer_name')
                raw_c = c_data.get('customer_code')
                resolved_code, resolved_name = customer_resolver.resolve(raw_n, raw_c)

                c_data['customer_code'] = resolved_code
                c_data['customer_name'] = resolved_name

                existing_c = customer_resolver.code_map.get(resolved_code)
                if existing_c:
                    for k, v in c_data.items():
                        if v is not None and v != '':
                            setattr(existing_c, k, v)
                else:
                    new_c = Customer(**c_data)
                    self.db.add(new_c)
                    customer_resolver.register_customer(new_c)
            stats['customers_upserted'] = len(customers_data)

        # ── 5. Upsert Receivables ────────────────────────────────────────────
        receivables_data = parsed.get('receivables', [])
        if receivables_data:
            existing_recs = {r.invoice_no: r for r in self.db.scalars(select(CustomerReceivable)).all()}
            for r_data in receivables_data:
                raw_n = r_data.get('customer_name')
                raw_c = r_data.get('customer_code')
                resolved_code, resolved_name = customer_resolver.resolve(raw_n, raw_c)

                r_data['customer_code'] = resolved_code
                r_data['customer_name'] = resolved_name

                inv = r_data['invoice_no']
                if inv in existing_recs:
                    for k, v in r_data.items():
                        setattr(existing_recs[inv], k, v)
                else:
                    self.db.add(CustomerReceivable(**r_data))
            stats['receivables_upserted'] = len(receivables_data)

        self.db.commit()

        # ── 6. Insert Sales History ──────────────────────────────────────────
        # product_name is stored as-is from MARG for traceability.
        # product_code is resolved to the canonical code via normalization.
        # customer_code and customer_name are resolved against existing customer master records.
        sales_records = []
        matched_customers = set()

        # Pre-load catalog unit costs to populate rate and amount if missing in sales report
        prod_costs = {
            p.product_code: p.unit_cost
            for p in self.db.scalars(select(Product)).all()
            if p.unit_cost and p.unit_cost > 0
        }

        for s_data in sales_data:
            raw_name = s_data.get('product_name', '')
            p_code = s_data['product_code']

            # Resolve code against catalog using pharma normalization
            resolved_code = self._resolve_code(p_code, raw_name)

            s_rec = dict(s_data)
            s_rec['product_code'] = resolved_code
            s_rec['product_name'] = raw_name
            s_rec['free_qty'] = float(s_data.get('free_qty') or 0.0)

            # Resolve customer code and customer name against customer master
            raw_c_name = s_data.get('customer_name', '')
            raw_c_code = s_data.get('customer_code')
            resolved_cust_code, resolved_cust_name = customer_resolver.resolve(raw_c_name, raw_c_code)

            # Fallback for rate & amount from product catalog
            if s_rec.get('rate', 0.0) <= 0:
                s_rec['rate'] = prod_costs.get(resolved_code, 0.0)
            if s_rec.get('amount', 0.0) <= 0 and s_rec.get('qty_sold', 0.0) > 0:
                s_rec['amount'] = round(s_rec['qty_sold'] * s_rec.get('rate', 0.0), 2)

            if resolved_cust_code:
                s_rec['customer_code'] = resolved_cust_code
                s_rec['customer_name'] = resolved_cust_name
                matched_customers.add(resolved_cust_code)

                # If customer is not yet in Customer master, create an active record
                if resolved_cust_code not in customer_resolver.code_map:
                    new_cust = Customer(
                        customer_code=resolved_cust_code,
                        customer_name=resolved_cust_name or resolved_cust_code,
                        group_name='General',
                        district='West Bengal',
                        salesperson='Sales Team',
                        status='ACTIVE',
                    )
                    self.db.add(new_cust)
                    customer_resolver.register_customer(new_cust)
                    stats['customers_upserted'] = stats.get('customers_upserted', 0) + 1

            sales_records.append(SalesHistory(**s_rec))

        if matched_customers:
            self.db.execute(
                delete(SalesHistory).where(
                    SalesHistory.customer_code.in_(list(matched_customers))
                )
            )

        if sales_records:
            self.db.add_all(sales_records)
            stats['sales_inserted'] += len(sales_records)
            stats['matched_customers'] = len(matched_customers)

        self.db.commit()

        # ── 7. Post-upload reorder & stock sync ──────────────────────────────
        # After committing all sales rows, recalculate reorder_point and current_stock
        # for every product that has sales history so KPI cards and proposals are fresh.
        if sales_data:
            stats['reorder_synced'] = self._sync_reorder_from_sales()

        audit(
            self.db,
            event_type='MARG_EXCEL_INGESTED',
            actor='user-upload',
            entity_type='file',
            entity_id=filename,
            details=stats,
        )
        self.db.commit()

        return stats

    def ingest_ordered_items_excel(self, file_content: bytes, filename: str) -> dict[str, Any]:
        """
        Ingests an Excel or CSV file containing already-ordered / pipeline items (Stock On Order).

        Guardrail:
        - If the Product table in the database is empty, immediately stops ingestion and raises:
          "Please upload the stock and sales report first."

        Processing:
        1. Reads Excel / CSV content.
        2. Identifies product name, order quantity, and composition columns.
        3. Uses PharmaProductMatcher to normalize pharma names and match to catalog products.
        4. Updates InventoryBatch.qty_on_order for each matched product (clearing previous on-order quantities first).
        5. Synchronizes ProcurementProposal.stock_on_order if proposals exist.
        6. Returns structured ingestion stats and lists of matched/unmatched items.
        """
        import io
        import pandas as pd
        from datetime import datetime
        from backend.app.services.pharma_matcher import PharmaProductMatcher

        # Guardrail: Must have catalog products already in database
        prod_count = self.db.scalar(select(func.count(Product.id))) or 0
        if prod_count == 0:
            raise ValueError("Please upload the stock and sales report first.")

        # Read file into DataFrame
        try:
            if filename.lower().endswith('.csv'):
                df = pd.read_csv(io.BytesIO(file_content))
            else:
                df = pd.read_excel(io.BytesIO(file_content))
        except Exception as e:
            raise ValueError(f"Could not read uploaded Excel/CSV file: {e}")

        if df.empty:
            raise ValueError("The uploaded file contains no data rows.")

        # Identify key columns flexibly
        cols = {str(c).strip().lower(): c for c in df.columns}

        name_col = None
        for cand in ['product name', 'product', 'item name', 'item', 'product_name', 'description']:
            if cand in cols:
                name_col = cols[cand]
                break
        if not name_col:
            name_col = df.columns[0]

        qty_col = None
        for cand in ['order qty', 'order quantity', 'ordered qty', 'qty', 'quantity', 'pending qty', 'po qty', 'qty_on_order']:
            if cand in cols:
                qty_col = cols[cand]
                break
        if not qty_col:
            for c_low, orig_col in cols.items():
                if 'qty' in c_low or 'quantity' in c_low or 'order' in c_low:
                    qty_col = orig_col
                    break

        comp_col = None
        for cand in ['composition', 'salt', 'formula', 'generic name', 'generic']:
            if cand in cols:
                comp_col = cols[cand]
                break

        status_col = None
        for cand in ['status', 'order status']:
            if cand in cols:
                status_col = cols[cand]
                break

        # Load all products from DB for pharma matching
        all_prods = self.db.scalars(select(Product)).all()
        matcher = PharmaProductMatcher(all_prods)

        # Clear previous pipeline quantities on batches to prevent double-counting on re-upload
        self.db.execute(update(InventoryBatch).values(qty_on_order=0.0))

        # Aggregate ordered quantities by matched product code
        ordered_by_code: dict[str, float] = {}
        matched_details: list[dict] = []
        unmatched_details: list[dict] = []

        for idx, row in df.iterrows():
            raw_name = str(row.get(name_col, '') or '').strip()
            if not raw_name or raw_name.lower() in ('nan', 'none', 'total', 'grand total'):
                continue

            # Parse quantity
            raw_qty = row.get(qty_col, 0) if qty_col else 0
            try:
                qty = float(pd.to_numeric(raw_qty, errors='coerce') or 0.0)
            except Exception:
                qty = 0.0

            # Check status if column exists (skip explicitly cancelled orders)
            if status_col:
                st_val = str(row.get(status_col, '') or '').strip().lower()
                if 'cancel' in st_val or 'reject' in st_val:
                    continue

            comp_val = str(row.get(comp_col, '') or '').strip() if comp_col else ""

            matched_prod, method, score = matcher.match(raw_name, comp_val)

            if matched_prod:
                p_code = matched_prod.product_code
                ordered_by_code[p_code] = ordered_by_code.get(p_code, 0.0) + qty
                matched_details.append({
                    'ordered_name': raw_name,
                    'matched_code': p_code,
                    'matched_name': matched_prod.product_name,
                    'order_qty': qty,
                    'method': method,
                    'confidence': score,
                })
            else:
                unmatched_details.append({
                    'ordered_name': raw_name,
                    'order_qty': qty,
                    'composition': comp_val,
                })

        # Update InventoryBatch with the on-order quantities
        for p_code, tot_qty in ordered_by_code.items():
            batches = self.db.scalars(
                select(InventoryBatch).where(InventoryBatch.product_code == p_code)
            ).all()

            if batches:
                primary_batch = max(batches, key=lambda b: getattr(b, 'expiry_date', None) or datetime.min)
                primary_batch.qty_on_order = tot_qty
            else:
                new_batch = InventoryBatch(
                    product_code=p_code,
                    batch_no='ON_ORDER',
                    qty_on_hand=0.0,
                    qty_on_order=tot_qty,
                    is_promo_material=False,
                )
                self.db.add(new_batch)

        # Synchronize proposals if any exist in the database
        for p_code, tot_qty in ordered_by_code.items():
            self.db.execute(
                update(ProcurementProposal)
                .where(ProcurementProposal.product_code == p_code)
                .values(stock_on_order=tot_qty)
            )

        self.db.commit()

        total_units = sum(ordered_by_code.values())
        stats = {
            'total_rows_processed': len(df),
            'matched_count': len(matched_details),
            'unmatched_count': len(unmatched_details),
            'unique_products_updated': len(ordered_by_code),
            'total_units_on_order': round(total_units, 2),
            'matched_items': matched_details,
            'unmatched_items': unmatched_details,
        }

        audit(
            self.db,
            event_type='ORDERED_ITEMS_INGESTED',
            actor='user-upload',
            entity_type='file',
            entity_id=filename,
            details={
                'total_rows': stats['total_rows_processed'],
                'matched': stats['matched_count'],
                'unmatched': stats['unmatched_count'],
                'total_units': stats['total_units_on_order'],
                'products_updated': stats['unique_products_updated'],
            },
        )
        self.db.commit()

        return stats


