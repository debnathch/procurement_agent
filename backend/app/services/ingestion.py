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
from typing import Any
from sqlalchemy.orm import Session
from sqlalchemy import delete, select, func, update
from backend.app.models.entities import (
    Product, Supplier, InventoryBatch, SalesHistory,
    ProcurementRun, ProcurementProposal, FeedbackEvent,
    Customer, CustomerReceivable
)
from backend.app.services.audit import audit
from backend.app.adapters.excel import MargExcelParser


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
            'duplicates_merged': 0,
            'reorder_synced': 0,
            'total_rows_parsed': (
                len(products_data) + len(batches_data) +
                len(suppliers_data) + len(sales_data)
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

        # ── 4. Insert Sales History ──────────────────────────────────────────
        # product_name is stored as-is from MARG for traceability.
        # product_code is resolved to the canonical code via normalization.
        sales_records = []
        for s_data in sales_data:
            raw_name = s_data.get('product_name', '')
            p_code = s_data['product_code']

            # Resolve code against catalog using pharma normalization
            resolved_code = self._resolve_code(p_code, raw_name)

            s_rec = dict(s_data)
            s_rec['product_code'] = resolved_code
            s_rec['product_name'] = raw_name
            sales_records.append(SalesHistory(**s_rec))

        if sales_records:
            self.db.add_all(sales_records)
            stats['sales_inserted'] += len(sales_records)

        self.db.commit()

        # ── 5. Upsert Customers ──────────────────────────────────────────────
        customers_data = parsed.get('customers', [])
        if customers_data:
            existing_custs = {c.customer_code: c for c in self.db.scalars(select(Customer)).all()}
            for c_data in customers_data:
                ccode = c_data['customer_code']
                if ccode in existing_custs:
                    for k, v in c_data.items():
                        if v is not None and v != '':
                            setattr(existing_custs[ccode], k, v)
                else:
                    new_c = Customer(**c_data)
                    self.db.add(new_c)
                    existing_custs[ccode] = new_c
            stats['customers_upserted'] = len(customers_data)

        # ── 6. Upsert Receivables ────────────────────────────────────────────
        receivables_data = parsed.get('receivables', [])
        if receivables_data:
            existing_recs = {r.invoice_no: r for r in self.db.scalars(select(CustomerReceivable)).all()}
            for r_data in receivables_data:
                inv = r_data['invoice_no']
                if inv in existing_recs:
                    for k, v in r_data.items():
                        setattr(existing_recs[inv], k, v)
                else:
                    self.db.add(CustomerReceivable(**r_data))
            stats['receivables_upserted'] = len(receivables_data)

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

