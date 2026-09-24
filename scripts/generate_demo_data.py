"""
generate_demo_data.py
Seed the SQLite database with realistic pharmaceutical demo data.
Run from the project root:
    python scripts/generate_demo_data.py
"""
import sys
import os
from pathlib import Path
from datetime import datetime, timedelta
import random

# Make sure project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app.core.database import SessionLocal, init_db
from backend.app.models.entities import (
    Product, Supplier, InventoryBatch, SalesHistory
)

random.seed(42)

SUPPLIERS = [
    dict(supplier_id='SUP001', supplier_name='MedPharma Distributors Pvt Ltd',
         contact_name='Rajesh Kumar', contact_email='rajesh@medpharma.in',
         lead_time_days=5, min_order_value=5000.0, reliability_score=0.95, is_active=True),
    dict(supplier_id='SUP002', supplier_name='HealthPlus Wholesale',
         contact_name='Priya Sharma', contact_email='priya@healthplus.in',
         lead_time_days=7, min_order_value=2000.0, reliability_score=0.88, is_active=True),
    dict(supplier_id='SUP003', supplier_name='Generic Meds Co',
         contact_name='Amit Singh', contact_email='amit@genericmeds.in',
         lead_time_days=10, min_order_value=1000.0, reliability_score=0.80, is_active=True),
]

PRODUCTS = [
    dict(product_code='MED001', product_name='Paracetamol 500mg Tabs (Strip of 10)',
         category='Analgesic', unit='strip', pack_size=10.0, min_order_qty=100.0,
         unit_cost=18.5, reorder_point=500.0, reorder_enabled=True, preferred_supplier_id='SUP001'),
    dict(product_code='MED002', product_name='Amoxicillin 250mg Caps (Strip of 10)',
         category='Antibiotic', unit='strip', pack_size=10.0, min_order_qty=50.0,
         unit_cost=45.0, reorder_point=200.0, reorder_enabled=True, preferred_supplier_id='SUP001'),
    dict(product_code='MED003', product_name='Metformin 500mg Tabs (Strip of 10)',
         category='Antidiabetic', unit='strip', pack_size=10.0, min_order_qty=100.0,
         unit_cost=22.0, reorder_point=400.0, reorder_enabled=True, preferred_supplier_id='SUP002'),
    dict(product_code='MED004', product_name='Atorvastatin 10mg Tabs (Strip of 10)',
         category='Cardiovascular', unit='strip', pack_size=10.0, min_order_qty=50.0,
         unit_cost=55.0, reorder_point=150.0, reorder_enabled=True, preferred_supplier_id='SUP002'),
    dict(product_code='MED005', product_name='Omeprazole 20mg Caps (Strip of 10)',
         category='Gastrology', unit='strip', pack_size=10.0, min_order_qty=100.0,
         unit_cost=30.0, reorder_point=300.0, reorder_enabled=True, preferred_supplier_id='SUP003'),
    dict(product_code='MED006', product_name='Cetirizine 10mg Tabs (Strip of 10)',
         category='Antihistamine', unit='strip', pack_size=10.0, min_order_qty=100.0,
         unit_cost=15.0, reorder_point=250.0, reorder_enabled=True, preferred_supplier_id='SUP001'),
    dict(product_code='MED007', product_name='Vitamin D3 60000 IU Capsules (Pack of 4)',
         category='Supplement', unit='pack', pack_size=4.0, min_order_qty=40.0,
         unit_cost=85.0, reorder_point=100.0, reorder_enabled=True, preferred_supplier_id='SUP002'),
    dict(product_code='MED008', product_name='Azithromycin 500mg Tabs (Strip of 3)',
         category='Antibiotic', unit='strip', pack_size=3.0, min_order_qty=30.0,
         unit_cost=120.0, reorder_point=90.0, reorder_enabled=True, preferred_supplier_id='SUP001'),
]

# Inventory: some products low stock, some near expiry
INVENTORY = [
    # product_code, batch_no, qty_on_hand, qty_on_order, days_until_expiry, unit_cost
    ('MED001', 'B2401', 80.0,  0.0,  180, 18.5),   # low stock → should trigger order
    ('MED002', 'B2402', 200.0, 0.0,  365, 45.0),
    ('MED003', 'B2403', 60.0,  0.0,  270, 22.0),   # low stock
    ('MED004', 'B2404', 300.0, 0.0,  400, 55.0),
    ('MED005', 'B2405', 50.0,  100.0, 200, 30.0),   # low, but on-order
    ('MED006', 'B2406', 20.0,  0.0,  30,  15.0),   # near expiry!
    ('MED007', 'B2407', 120.0, 0.0,  500, 85.0),
    ('MED008', 'B2408', 15.0,  0.0,  90,  120.0),  # very low
]


def seed_sales(db, product_code: str, avg_daily: float, days: int = 90):
    """Generate synthetic daily sales history."""
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(days, 0, -1):
        qty = max(0.0, round(random.gauss(avg_daily, avg_daily * 0.3), 1))
        db.add(SalesHistory(
            product_code=product_code,
            sale_date=today - timedelta(days=i),
            qty_sold=qty,
            channel='retail',
        ))


def main():
    print('Initialising database and creating tables...')
    init_db()

    db = SessionLocal()
    try:
        # Clear existing demo data
        for model in [SalesHistory, InventoryBatch, Product, Supplier]:
            db.query(model).delete()
        db.commit()

        # Suppliers
        print('Seeding suppliers...')
        for s in SUPPLIERS:
            db.add(Supplier(**s))
        db.commit()

        # Products
        print('Seeding products...')
        for p in PRODUCTS:
            db.add(Product(**p))
        db.commit()

        # Inventory batches
        print('Seeding inventory batches...')
        now = datetime.utcnow()
        for (pcode, batch_no, qty_on_hand, qty_on_order, days_exp, ucost) in INVENTORY:
            expiry = now + timedelta(days=days_exp) if days_exp > 0 else None
            db.add(InventoryBatch(
                product_code=pcode,
                batch_no=batch_no,
                qty_on_hand=qty_on_hand,
                qty_on_order=qty_on_order,
                expiry_date=expiry,
                unit_cost=ucost,
            ))
        db.commit()

        # Sales history (avg daily demand per product)
        print('Seeding 90-day sales history...')
        avg_demand = {
            'MED001': 15.0,
            'MED002': 5.0,
            'MED003': 10.0,
            'MED004': 4.0,
            'MED005': 8.0,
            'MED006': 7.0,
            'MED007': 3.0,
            'MED008': 2.5,
        }
        for pcode, avg in avg_demand.items():
            seed_sales(db, pcode, avg)
        db.commit()

        print()
        print('✅  Demo data seeded successfully!')
        print('   Products  :', len(PRODUCTS))
        print('   Suppliers :', len(SUPPLIERS))
        print('   Inv batches:', len(INVENTORY))
        print()
        print('Now start the backend:')
        print('   uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000')

    except Exception as exc:
        db.rollback()
        print(f'❌  Error: {exc}')
        raise
    finally:
        db.close()


if __name__ == '__main__':
    main()
