import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import Base
from backend.app.models.entities import Product, Supplier, InventoryBatch, SalesHistory, ProcurementProposal
from backend.app.main import (
    list_manufacturers,
    trigger_run,
    list_proposals,
    list_no_reorder_products,
    RunRequest,
)

# Use in-memory SQLite database
SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()

    # Create Suppliers
    s1 = Supplier(
        supplier_id="SUP-001",
        supplier_name="Apex Pharma Distributors",
        lead_time_days=45,
        min_order_value=0.0,
        reliability_score=0.95,
        is_active=True,
    )
    session.add(s1)

    # Create Products across two distinct manufacturers
    p1 = Product(
        product_code="MED-CIPRO-500",
        product_name="Ciprofloxacin 500mg Tablets",
        category="Antibiotic",
        company="Cipla Ltd",
        manufacturer="Cipla Pharma",
        unit="Strip",
        pack_size=10.0,
        min_order_qty=10.0,
        unit_cost=50.0,
        reorder_enabled=True,
        preferred_supplier_id="SUP-001",
        supplier_name="Apex Pharma Distributors",
    )
    p2 = Product(
        product_code="MED-AZI-250",
        product_name="Azithromycin 250mg Tablets",
        category="Antibiotic",
        company="Sun Pharma",
        manufacturer="Sun Diagnostics",
        unit="Strip",
        pack_size=6.0,
        min_order_qty=6.0,
        unit_cost=75.0,
        reorder_enabled=True,
        preferred_supplier_id="SUP-001",
        supplier_name="Apex Pharma Distributors",
    )
    session.add_all([p1, p2])

    today = datetime.utcnow()
    for i in range(10):
        session.add(SalesHistory(
            product_code="MED-CIPRO-500",
            product_name="Ciprofloxacin 500mg Tablets",
            qty_sold=15.0,
            sale_date=today - timedelta(days=i),
        ))
        session.add(SalesHistory(
            product_code="MED-AZI-250",
            product_name="Azithromycin 250mg Tablets",
            qty_sold=2.0,
            sale_date=today - timedelta(days=i),
        ))

    # Cipro has low stock -> triggers proposal
    b1 = InventoryBatch(
        product_code="MED-CIPRO-500",
        batch_no="B-CIPRO-01",
        company="Cipla Ltd",
        manufacturer="Cipla Pharma",
        qty_on_hand=1.0,
        unit_cost=50.0,
        expiry_date=today + timedelta(days=365),
    )
    # Azithromycin has surplus stock -> qualifies for no-reorder
    b2 = InventoryBatch(
        product_code="MED-AZI-250",
        batch_no="B-AZI-01",
        company="Sun Pharma",
        manufacturer="Sun Diagnostics",
        qty_on_hand=500.0,
        unit_cost=75.0,
        expiry_date=today + timedelta(days=365),
    )
    session.add_all([b1, b2])
    session.commit()

    yield session

    session.close()
    Base.metadata.drop_all(bind=engine)


def test_list_manufacturers_endpoint(db):
    """Verify distinct manufacturers endpoint returns clean list."""
    mfrs = list_manufacturers(db=db)
    assert "Cipla Pharma" in mfrs
    assert "Sun Diagnostics" in mfrs
    assert "General" not in mfrs


def test_trigger_run_with_custom_policy_parameters(db):
    """
    Verify trigger_run recalculates target stock and order quantity with dynamic
    lead_time_days, review_days, and safety_days.
    """
    # 1. Run with baseline policy: lead=20, review=5, safety=2 -> Coverage = 27 days
    req1 = RunRequest(lead_time_days=20, review_days=5, safety_days=2)
    res1 = trigger_run(req=req1, db=db)
    assert res1["lead_time_days"] == 20
    assert res1["review_days"] == 5
    assert res1["safety_days"] == 2

    proposals1 = list_proposals(db=db)
    cipro1 = next((p for p in proposals1 if p["product_code"] == "MED-CIPRO-500"), None)
    assert cipro1 is not None
    assert cipro1["lead_time_days"] == 20
    assert cipro1["review_days"] == 5
    assert cipro1["safety_days"] == 2
    assert cipro1["target_coverage_days"] == 27
    qty1 = cipro1["recommended_qty"]

    # 2. Run with higher policy: lead=40, review=14, safety=6 -> Coverage = 60 days
    req2 = RunRequest(lead_time_days=40, review_days=14, safety_days=6)
    res2 = trigger_run(req=req2, db=db)
    assert res2["lead_time_days"] == 40
    assert res2["review_days"] == 14
    assert res2["safety_days"] == 6

    proposals2 = list_proposals(db=db)
    # The newest run's proposal for Cipro
    cipro2 = next((p for p in proposals2 if p["product_code"] == "MED-CIPRO-500" and p["run_id"] == res2["run_id"]), None)
    assert cipro2 is not None
    assert cipro2["lead_time_days"] == 40
    assert cipro2["review_days"] == 14
    assert cipro2["safety_days"] == 6
    assert cipro2["target_coverage_days"] == 60
    qty2 = cipro2["recommended_qty"]

    # Target stock formula: Demand * (Lead + Review + Safety)
    # Higher target coverage days directly produces higher recommended order quantity
    assert qty2 > qty1


def test_trigger_run_with_manufacturer_filter(db):
    """Verify trigger_run restricts evaluation to the specified manufacturer."""
    req = RunRequest(
        lead_time_days=30,
        review_days=7,
        safety_days=3,
        manufacturer="Cipla Pharma",
    )
    res = trigger_run(req=req, db=db)
    assert res["manufacturer"] == "Cipla Pharma"

    proposals = list_proposals(db=db)
    assert len(proposals) > 0
    for p in proposals:
        assert p["manufacturer"] == "Cipla Pharma"


def test_no_reorder_endpoint_runtime_policy(db):
    """Verify list_no_reorder_products calculates dynamic coverage and surplus stock."""
    # Azithromycin has 500 units usable stock on hand, demand = 2.0/day
    # With lead=20, review=7, safety=3 -> Target coverage = 30 days -> Target stock = 60 units
    # Usable stock 500 > Target stock 60 -> qualifies for no-reorder with surplus = 440 units
    items = list_no_reorder_products(
        lead_time_days=20,
        review_days=7,
        safety_days=3,
        db=db,
    )
    azi = next((item for item in items if item["product_code"] == "MED-AZI-250"), None)
    assert azi is not None
    assert azi["lead_time_days"] == 20
    assert azi["review_days"] == 7
    assert azi["safety_days"] == 3
    assert azi["target_coverage_days"] == 30
    assert azi["surplus_qty"] > 0

    # Manufacturer filter check on no-reorder
    items_filtered = list_no_reorder_products(
        lead_time_days=20,
        manufacturer="Sun Diagnostics",
        db=db,
    )
    assert len(items_filtered) > 0
    for item in items_filtered:
        assert item["manufacturer"] == "Sun Diagnostics"
