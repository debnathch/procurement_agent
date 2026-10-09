"""
Tests for Ingestion of Already Ordered Items (Pipeline / Stock On Order)
and Pro-Pharmacist NLP Product Matcher.
"""
import io
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.app.models.entities import Base, Product, InventoryBatch
from backend.app.services.ingestion import IngestionService
from backend.app.services.pharma_matcher import PharmaProductMatcher


@pytest.fixture
def test_db():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_empty_product_table_guardrail(test_db):
    """
    If the Product table is empty, ingestion must immediately fail with:
    'Please upload the stock and sales report first.'
    """
    ingestion = IngestionService(test_db)
    df = pd.DataFrame([
        {'Product Name': 'DR-LIN 600 TAB', 'Order Qty': 100}
    ])
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)

    with pytest.raises(ValueError) as excinfo:
        ingestion.ingest_ordered_items_excel(buf.getvalue(), 'orders.xlsx')

    assert "Please upload the stock and sales report first." in str(excinfo.value)


def test_pharma_matcher_and_on_order_update(test_db):
    """
    Tests pharma NLP normalization, matching, and updating qty_on_order in batches.
    """
    # 1. Seed products
    p1 = Product(product_code="A00673", product_name="DR-LIN-600 TAB 5X1X10", pack_size=10.0, unit_cost=250.0)
    p2 = Product(product_code="A00163", product_name="AC-BEN SP TABLET 10X10", pack_size=100.0, unit_cost=92.0)
    p3 = Product(product_code="A00130", product_name="BRAXIN-100 ORAL SUSP-100ML 100ml", pack_size=1.0, unit_cost=45.0)
    test_db.add_all([p1, p2, p3])

    # Seed an existing batch for p1
    b1 = InventoryBatch(product_code="A00673", batch_no="B01", qty_on_hand=50.0, qty_on_order=0.0)
    test_db.add(b1)
    test_db.commit()

    # 2. Ingest ordered Excel
    df = pd.DataFrame([
        {'Product Name': 'DR-LIN 600 TAB', 'Order Qty': 200, 'Status': 'Initiated'},
        {'Product Name': 'AC-Ben-SP', 'Order Qty': 500, 'Status': 'Initiated'},
        {'Product Name': 'BRAXIN-100 ml', 'Order Qty': 300, 'Status': 'Initiated'},
        {'Product Name': 'COMPLETELY_NEW_DRUG_XYZ', 'Order Qty': 50, 'Status': 'Initiated'},
    ])
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)

    ingestion = IngestionService(test_db)
    stats = ingestion.ingest_ordered_items_excel(buf.getvalue(), 'initiated_orders.xlsx')

    assert stats['matched_count'] == 3
    assert stats['unmatched_count'] == 1
    assert stats['unique_products_updated'] == 3
    assert stats['total_units_on_order'] == 1000.0

    # Verify batch for p1 was updated
    test_db.refresh(b1)
    assert b1.qty_on_order == 200.0

    # Verify dedicated batch was created for p2 (which had no prior batch)
    b2 = test_db.query(InventoryBatch).filter_by(product_code="A00163").first()
    assert b2 is not None
    assert b2.qty_on_order == 500.0
