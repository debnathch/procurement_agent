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
from sqlalchemy import delete, select
from backend.app.models.entities import (
    Product, Supplier, InventoryBatch, SalesHistory,
    ProcurementRun, ProcurementProposal, FeedbackEvent
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

    def _build_name_index(self) -> None:
        """Load all existing product names → codes into the local dedup cache."""
        from backend.app.adapters.excel import pharma_canonical_key
        rows = self.db.execute(
            select(Product.product_code, Product.product_name, Product.category)
        ).all()
        sorted_rows = sorted(rows, key=lambda r: 1 if (r.category or '').startswith('ZZZZ') else 0)
        self._name_to_code = {}
        for row in sorted_rows:
            raw_key = row.product_name.strip().upper()
            self._name_to_code[raw_key] = row.product_code
            pkey = pharma_canonical_key(row.product_name)
            if pkey:
                self._name_to_code[pkey] = row.product_code

    def _resolve_code(self, incoming_code: str, incoming_name: str) -> str:
        """
        Returns the canonical product_code to use for this product.

        Priority:
          1. Exact code already in DB   → use it (normal upsert).
          2. Same product_name in DB    → reuse existing code (deduplication).
          3. Pharma formulation match   → reuse existing code (packaging format variation).
          4. Neither                    → incoming_code is new, use it.
        """
        if self.db.get(Product, incoming_code):
            return incoming_code
        name_key = incoming_name.strip().upper()
        if name_key in self._name_to_code:
            return self._name_to_code[name_key]
        from backend.app.adapters.excel import pharma_canonical_key
        pkey = pharma_canonical_key(incoming_name)
        if pkey and pkey in self._name_to_code:
            return self._name_to_code[pkey]
        return incoming_code

    def purge_all_data(self) -> dict[str, int]:
        """
        Clears all operational procurement data.
        Ensures a completely fresh start for newly uploaded data.
        """
        del_fb   = self.db.execute(delete(FeedbackEvent)).rowcount
        del_prop = self.db.execute(delete(ProcurementProposal)).rowcount
        del_runs = self.db.execute(delete(ProcurementRun)).rowcount
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
            'deleted_batches': del_batch,
            'deleted_sales': del_sales,
            'deleted_proposals': del_prop,
        }

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
            'total_rows_parsed': (
                len(products_data) + len(batches_data) +
                len(suppliers_data) + len(sales_data)
            ),
        }

        # ── 1. Upsert Suppliers ───────────────────────────────────────────────
        for s_data in suppliers_data:
            existing = self.db.get(Supplier, s_data['supplier_id'])
            if existing:
                for k, v in s_data.items():
                    setattr(existing, k, v)
            else:
                self.db.add(Supplier(**s_data))
            stats['suppliers_upserted'] += 1

        self.db.flush()

        # ── 2. Upsert Products with name-based deduplication ─────────────────
        # code_remap: incoming_code → canonical_code for downstream remapping
        code_remap: dict[str, str] = {}

        has_stock_batches = len(batches_data) > 0

        for p_data in products_data:
            incoming_code = p_data['product_code']
            incoming_name = p_data.get('product_name', incoming_code)
            canonical_code = self._resolve_code(incoming_code, incoming_name)

            if canonical_code != incoming_code:
                # Duplicate detected — remap incoming code to canonical
                code_remap[incoming_code] = canonical_code
                stats['duplicates_merged'] += 1
                # Optionally refresh cost/reorder on the canonical record (stock sheets only)
                existing = self.db.get(Product, canonical_code)
                if existing:
                    if has_stock_batches and p_data.get('unit_cost') and p_data['unit_cost'] > 0:
                        existing.unit_cost = p_data['unit_cost']
                    if p_data.get('reorder_point') and p_data['reorder_point'] > 0:
                        existing.reorder_point = p_data['reorder_point']
                    if p_data.get('company') and p_data['company'] != 'General':
                        existing.company = p_data['company']
            else:
                existing = self.db.get(Product, canonical_code)
                if existing:
                    for k, v in p_data.items():
                        if k == 'unit_cost' and (not has_stock_batches or v is None or v <= 0):
                            continue  # Preserve existing Cost Price from stock report
                        setattr(existing, k, v)
                else:
                    self.db.add(Product(**p_data))
                    # Register in cache so later rows in this file see it
                    self._name_to_code[incoming_name.strip().upper()] = canonical_code

            stats['products_upserted'] += 1

        self.db.flush()

        # ── 3. Replace Inventory Batches (apply code_remap first) ─────────────
        for b_data in batches_data:
            if b_data['product_code'] in code_remap:
                b_data['product_code'] = code_remap[b_data['product_code']]

        batch_product_codes = list({b['product_code'] for b in batches_data})
        if batch_product_codes:
            for p_code in batch_product_codes:
                if not self.db.get(Product, p_code):
                    matching_batch = next((b for b in batches_data if b['product_code'] == p_code), None)
                    batch_comp = matching_batch.get('company') if matching_batch else 'General'
                    self.db.add(Product(
                        product_code=p_code,
                        product_name=p_code,
                        category='General',
                        company=batch_comp or 'General',
                        unit='unit',
                        pack_size=1.0,
                        unit_cost=0.0,
                    ))
            self.db.flush()

            self.db.execute(
                delete(InventoryBatch).where(
                    InventoryBatch.product_code.in_(batch_product_codes)
                )
            )
            for b_data in batches_data:
                self.db.add(InventoryBatch(**b_data))
                stats['batches_inserted'] += 1

        # ── 4. Insert Sales History (apply code_remap first) ──────────────────
        for s_data in sales_data:
            if s_data['product_code'] in code_remap:
                s_data['product_code'] = code_remap[s_data['product_code']]

            p_code = s_data['product_code']
            if not self.db.get(Product, p_code):
                self.db.add(Product(
                    product_code=p_code,
                    product_name=p_code,
                    category='Imported Demand',
                    unit='unit',
                    pack_size=1.0,
                    unit_cost=0.0,
                ))
                self.db.flush()

            self.db.add(SalesHistory(**s_data))
            stats['sales_inserted'] += 1

        self.db.commit()

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
