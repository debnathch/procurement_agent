import pytest
from datetime import datetime

from backend.app.core.database import SessionLocal
from backend.app.models.entities import Product, ProcurementProposal
from backend.app.main import (
    get_orders_by_category_and_division,
    list_procurement_divisions,
    list_procurement_categories,
    get_divisions_categories_summary,
)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def seed_test_proposals(db):
    # Seed distinct products with categories and divisions
    p1 = Product(
        product_code="TEST-DIV-001",
        product_name="CEFIX-200 TAB",
        category="Tablets / Oral Solids",
        company="BENGAL REMEDIES",
        manufacturer="ORISON",
        unit_cost=50.0,
        current_stock=10.0,
    )
    p2 = Product(
        product_code="TEST-DIV-002",
        product_name="AZITHRO-500 TAB",
        category="Tablets / Oral Solids",
        company="BENGAL REMEDIES",
        manufacturer="JMH",
        unit_cost=80.0,
        current_stock=5.0,
    )
    p3 = Product(
        product_code="TEST-DIV-003",
        product_name="COUGH-CARE SYRUP 100ML",
        category="Syrups & Liquids",
        company="GENIAL LIFECARE",
        manufacturer="GENIAL",
        unit_cost=30.0,
        current_stock=0.0,
    )
    db.add_all([p1, p2, p3])
    db.commit()

    prop1 = ProcurementProposal(
        run_id="test_run_agent_api",
        product_code="TEST-DIV-001",
        product_name="CEFIX-200 TAB",
        company="BENGAL REMEDIES",
        manufacturer="ORISON",
        recommended_qty=150.0,  # High urgency (> 100)
        unit_cost=50.0,
        estimated_value=7500.0,
        avg_daily_demand=3.0,
        lead_time_days=45,
        stock_on_hand=10.0,
        stock_on_order=0.0,
        status="PENDING",
        idempotency_key="test_prop_1",
    )
    prop2 = ProcurementProposal(
        run_id="test_run_agent_api",
        product_code="TEST-DIV-002",
        product_name="AZITHRO-500 TAB",
        company="BENGAL REMEDIES",
        manufacturer="JMH",
        recommended_qty=50.0,  # Normal urgency (<= 100)
        unit_cost=80.0,
        estimated_value=4000.0,
        avg_daily_demand=1.2,
        lead_time_days=45,
        stock_on_hand=5.0,
        stock_on_order=0.0,
        status="PENDING",
        idempotency_key="test_prop_2",
    )
    prop3 = ProcurementProposal(
        run_id="test_run_agent_api",
        product_code="TEST-DIV-003",
        product_name="COUGH-CARE SYRUP 100ML",
        company="GENIAL LIFECARE",
        manufacturer="GENIAL",
        recommended_qty=200.0,
        unit_cost=30.0,
        estimated_value=6000.0,
        avg_daily_demand=4.0,
        lead_time_days=30,
        stock_on_hand=0.0,
        stock_on_order=0.0,
        status="PENDING",
        idempotency_key="test_prop_3",
    )
    db.add_all([prop1, prop2, prop3])
    db.commit()

    yield

    # Clean up test rows
    db.query(ProcurementProposal).filter(
        ProcurementProposal.product_code.in_(["TEST-DIV-001", "TEST-DIV-002", "TEST-DIV-003"])
    ).delete(synchronize_session=False)
    db.query(Product).filter(
        Product.product_code.in_(["TEST-DIV-001", "TEST-DIV-002", "TEST-DIV-003"])
    ).delete(synchronize_session=False)
    db.commit()


def test_get_orders_by_category_and_division(db, seed_test_proposals):
    # 1. Fetch with division filter
    res = get_orders_by_category_and_division(
        division="BENGAL REMEDIES",
        db=db,
    )
    assert res.status == "success"
    assert res.metadata["total_matching_products"] >= 2
    codes = [i.product_code for i in res.items]
    assert "TEST-DIV-001" in codes
    assert "TEST-DIV-002" in codes
    assert "TEST-DIV-003" not in codes

    # Check urgency and red flag logic
    item1 = next(i for i in res.items if i.product_code == "TEST-DIV-001")
    assert item1.division == "BENGAL REMEDIES"
    assert item1.category == "Tablets / Oral Solids"
    assert item1.order_urgency == "HIGH"
    assert item1.order_qty_flag == "🚩 > 100 (High Qty)"

    item2 = next(i for i in res.items if i.product_code == "TEST-DIV-002")
    assert item2.order_urgency == "NORMAL"
    assert item2.order_qty_flag == "🟡 ≤ 100 (Normal Qty)"

    # 2. Filter by category
    res_cat = get_orders_by_category_and_division(
        category="Syrups",
        db=db,
    )
    codes_cat = [i.product_code for i in res_cat.items]
    assert "TEST-DIV-003" in codes_cat
    assert "TEST-DIV-001" not in codes_cat

    # 3. Filter by urgency=high
    res_urg = get_orders_by_category_and_division(
        division="BENGAL",
        urgency="high",
        db=db,
    )
    codes_urg = [i.product_code for i in res_urg.items]
    assert "TEST-DIV-001" in codes_urg
    assert "TEST-DIV-002" not in codes_urg

    # 4. Search keyword
    res_search = get_orders_by_category_and_division(
        search="AZITHRO",
        db=db,
    )
    assert len(res_search.items) == 1
    assert res_search.items[0].product_code == "TEST-DIV-002"


def test_list_procurement_divisions(db, seed_test_proposals):
    data = list_procurement_divisions(db=db)
    assert data["status"] == "success"
    assert data["total_divisions"] > 0
    div_names = [d["division"] for d in data["divisions"]]
    assert "BENGAL REMEDIES" in div_names
    assert "GENIAL LIFECARE" in div_names

    bengal = next(d for d in data["divisions"] if d["division"] == "BENGAL REMEDIES")
    assert bengal["pending_order_count"] >= 2
    assert bengal["total_recommended_qty"] >= 200.0


def test_list_procurement_categories(db, seed_test_proposals):
    data = list_procurement_categories(db=db)
    assert data["status"] == "success"
    assert data["total_categories"] > 0
    cat_names = [c["category"] for c in data["categories"]]
    assert any("Tablets" in c for c in cat_names)


def test_divisions_categories_summary(db, seed_test_proposals):
    data = get_divisions_categories_summary(db=db)
    assert data["status"] == "success"
    assert "divisions" in data
    bengal = next(d for d in data["divisions"] if d["division"] == "BENGAL REMEDIES")
    assert len(bengal["categories"]) > 0
