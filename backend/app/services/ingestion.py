"""
MARG Excel Ingestion Service

Takes parsed data structures from MargExcelParser and commits them to the database.
Handles database purge on fresh uploads, and upserts Products, Suppliers,
InventoryBatches, and SalesHistory with foreign key integrity.
"""
from typing import Any
from sqlalchemy.orm import Session
from sqlalchemy import delete
from backend.app.models.entities import (
    Product, Supplier, InventoryBatch, SalesHistory,
    ProcurementRun, ProcurementProposal, FeedbackEvent
)
from backend.app.services.audit import audit
from backend.app.adapters.excel import MargExcelParser


class IngestionService:
    def __init__(self, db: Session):
        self.db = db

    def purge_all_data(self) -> dict[str, int]:
        """
        Clears all operational procurement data:
        FeedbackEvent, ProcurementProposal, ProcurementRun,
        InventoryBatch, SalesHistory, Product, Supplier.
        Ensures a completely fresh start for newly uploaded data.
        """
        del_fb = self.db.execute(delete(FeedbackEvent)).rowcount
        del_prop = self.db.execute(delete(ProcurementProposal)).rowcount
        del_runs = self.db.execute(delete(ProcurementRun)).rowcount
        del_batch = self.db.execute(delete(InventoryBatch)).rowcount
        del_sales = self.db.execute(delete(SalesHistory)).rowcount
        del_prod = self.db.execute(delete(Product)).rowcount
        del_sup = self.db.execute(delete(Supplier)).rowcount
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
        clear_existing: bool = False
    ) -> dict[str, Any]:
        """
        Parses MARG Excel / CSV and updates products, inventory batches, suppliers, and sales history.
        If clear_existing is True, completely purges previous database records before importing.
        """
        purged_counts: dict[str, int] = {}
        if clear_existing:
            purged_counts = self.purge_all_data()

        parsed = MargExcelParser.parse_file(file_content)

        products_data = parsed['products']
        batches_data = parsed['inventory_batches']
        suppliers_data = parsed['suppliers']
        sales_data = parsed['sales_history']

        stats = {
            'database_cleared': clear_existing,
            'purged_counts': purged_counts,
            'products_upserted': 0,
            'suppliers_upserted': 0,
            'batches_inserted': 0,
            'sales_inserted': 0,
            'total_rows_parsed': len(products_data) + len(batches_data) + len(suppliers_data) + len(sales_data),
        }

        # 1. Upsert Suppliers
        for s_data in suppliers_data:
            existing = self.db.get(Supplier, s_data['supplier_id'])
            if existing:
                for k, v in s_data.items():
                    setattr(existing, k, v)
            else:
                self.db.add(Supplier(**s_data))
            stats['suppliers_upserted'] += 1

        self.db.flush()

        # 2. Upsert Products
        for p_data in products_data:
            existing = self.db.get(Product, p_data['product_code'])
            if existing:
                for k, v in p_data.items():
                    setattr(existing, k, v)
            else:
                self.db.add(Product(**p_data))
            stats['products_upserted'] += 1

        self.db.flush()

        # 3. Replace Inventory Batches for the updated products to maintain fresh stock position
        batch_product_codes = list({b['product_code'] for b in batches_data})
        if batch_product_codes:
            for p_code in batch_product_codes:
                if not self.db.get(Product, p_code):
                    self.db.add(Product(
                        product_code=p_code,
                        product_name=p_code,
                        category='General',
                        unit='unit',
                        pack_size=1.0,
                        unit_cost=0.0,
                    ))
            self.db.flush()

            self.db.execute(
                delete(InventoryBatch).where(InventoryBatch.product_code.in_(batch_product_codes))
            )
            for b_data in batches_data:
                self.db.add(InventoryBatch(**b_data))
                stats['batches_inserted'] += 1

        # 4. Insert Sales History if present (ensure product exists for foreign key constraint)
        for s_data in sales_data:
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

        # Audit event
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
