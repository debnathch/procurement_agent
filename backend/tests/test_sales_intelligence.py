"""
Tests for Sales Intelligence & Customer Analytics.
"""
from datetime import datetime, timedelta
import io
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import Base
from backend.app.models.entities import (
    Customer, CustomerReceivable, SalesHistory, InventoryBatch, Product
)
from backend.app.services.sales_intelligence import SalesIntelligenceService
from backend.app.services.ingestion import IngestionService


def test_sales_intelligence_customer_summary_and_ageing():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    now = datetime.utcnow()

    # 1. Add Customer
    c = Customer(
        customer_code='CUST-ABC',
        customer_name='ABC MEDICAL HALL',
        district='Kolkata',
        salesperson='A. Sharma',
        credit_limit=50000.0,
    )
    session.add(c)

    # 2. Add Sales History lines
    s1 = SalesHistory(
        customer_code='CUST-ABC',
        customer_name='ABC MEDICAL HALL',
        product_code='MED-001',
        product_name='AMOXY-500 TAB',
        invoice_no='INV-101',
        qty_sold=50.0,
        rate=100.0,
        amount=5000.0,
        sale_date=now - timedelta(days=15),
    )
    s2 = SalesHistory(
        customer_code='CUST-ABC',
        customer_name='ABC MEDICAL HALL',
        product_code='MED-002',
        product_name='PARACET-650',
        invoice_no='INV-102',
        qty_sold=20.0,
        rate=50.0,
        amount=1000.0,
        sale_date=now - timedelta(days=60),
    )
    s3 = SalesHistory(
        customer_code='CUST-ABC',
        customer_name='ABC MEDICAL HALL',
        product_code='MED-001',
        product_name='AMOXY-500 TAB',
        invoice_no='INV-103',
        qty_sold=10.0,
        rate=100.0,
        amount=1000.0,
        sale_date=now - timedelta(days=120),
    )
    session.add_all([s1, s2, s3])

    # 3. Add Receivables
    r1 = CustomerReceivable(
        customer_code='CUST-ABC',
        customer_name='ABC MEDICAL HALL',
        invoice_no='INV-101',
        invoice_amount=5000.0,
        outstanding_amount=2500.0,
        days_due=15,
        ageing_bucket='1–30 Days',
        invoice_date=now - timedelta(days=45),
        due_date=now - timedelta(days=15),
    )
    r2 = CustomerReceivable(
        customer_code='CUST-ABC',
        customer_name='ABC MEDICAL HALL',
        invoice_no='INV-099',
        invoice_amount=8000.0,
        outstanding_amount=8000.0,
        days_due=67,
        ageing_bucket='61–90 Days',
        invoice_date=now - timedelta(days=97),
        due_date=now - timedelta(days=67),
    )
    session.add_all([r1, r2])

    # 4. Add near-expiry inventory batch for MED-001 (bought by customer)
    b_near = InventoryBatch(
        product_code='MED-001',
        batch_no='B123',
        qty_on_hand=50.0,
        expiry_date=now + timedelta(days=55),
        unit_cost=80.0,
    )
    session.add(b_near)
    session.commit()

    svc = SalesIntelligenceService(session)

    # Test list_customers
    cust_list = svc.list_customers()
    assert len(cust_list) == 1
    assert cust_list[0]['customer_code'] == 'CUST-ABC'
    assert cust_list[0]['total_sales'] == 7000.0
    assert cust_list[0]['current_dues'] == 10500.0
    assert cust_list[0]['oldest_due_days'] == 67

    # Test get_customer_summary
    summary = svc.get_customer_summary('CUST-ABC')
    assert summary['customer_name'] == 'ABC MEDICAL HALL'
    assert summary['total_sales'] == 7000.0
    assert summary['sales_30d'] == 5000.0
    assert summary['sales_90d'] == 6000.0
    assert summary['current_outstanding'] == 10500.0
    assert summary['oldest_due_days'] == 67
    assert summary['oldest_due_invoice'] == 'INV-099'
    assert summary['ageing']['1_30'] == 2500.0
    assert summary['ageing']['61_90'] == 8000.0

    # Test recommendations (near-expiry opportunity for MED-001)
    recs = svc.get_customer_recommendations('CUST-ABC')
    opps = recs['near_expiry_opportunities']
    assert len(opps) == 1
    assert opps[0]['product_code'] == 'MED-001'
    assert opps[0]['batch_no'] == 'B123'
    assert opps[0]['days_remaining'] == 54 or opps[0]['days_remaining'] == 55
    assert opps[0]['suggested_qty'] > 0
    session.close()


def test_sales_seed_demo_data_and_service():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    # 1. Seed demo data
    res = SalesIntelligenceService.seed_demo_data(session)
    assert res['customers'] >= 3
    assert res['receivables'] >= 3
    assert res['sales_lines'] >= 3

    # 2. List customers
    svc = SalesIntelligenceService(session)
    customers = svc.list_customers()
    assert len(customers) >= 3

    abc = next((c for c in customers if 'ABC' in c['customer_name']), None)
    assert abc is not None
    assert abc['total_sales'] > 0
    assert abc['current_dues'] > 0

    # 3. Get customer summary
    summary = svc.get_customer_summary(abc['customer_code'])
    assert summary['customer_code'] == abc['customer_code']
    assert summary['total_sales'] > 0
    assert summary['current_outstanding'] > 0
    assert 'ageing' in summary

    # 4. Get customer recommendations
    recs = svc.get_customer_recommendations(abc['customer_code'])
    session.close()


def test_customer_group_ingestion_filtering_and_purge():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    # 1. Simulate MARG Excel upload with Ledger, Group, Debit, Credit columns
    df = pd.DataFrame([
        {'Ledger': 'CITY RETAIL CHEMISTS', 'Group': 'RETAIL', 'Debit': 120000.0, 'Credit': 90000.0, 'District': 'Kolkata'},
        {'Ledger': 'METRO PHARMA WHOLESALE', 'Group': 'WHOLESALE', 'Debit': 450000.0, 'Credit': 400000.0, 'District': 'Howrah'},
        {'Ledger': 'APOLLO HOSPITAL DISPENSARY', 'Group': 'HOSPITAL', 'Debit': 300000.0, 'Credit': 300000.0, 'District': 'North 24 Pgs'},
        {'Ledger': 'CORNER MEDICAL STORE', 'Group': 'RETAIL', 'Debit': 75000.0, 'Credit': 50000.0, 'District': 'Hooghly'},
    ])
    buf = io.BytesIO()
    df.to_excel(buf, index=False)

    ingest_svc = IngestionService(session)
    stats = ingest_svc.ingest_excel(buf.getvalue(), filename='Customer_Ledger_Groups.xlsx')
    assert stats['customers_upserted'] == 4

    # 2. Test group extraction & listing
    sales_svc = SalesIntelligenceService(session)
    groups = sales_svc.list_customer_groups()
    assert 'RETAIL' in groups
    assert 'WHOLESALE' in groups
    assert 'HOSPITAL' in groups

    # 3. Test filtering by group
    retail_custs = sales_svc.list_customers(group='RETAIL')
    assert len(retail_custs) == 2
    for c in retail_custs:
        assert c['group_name'] == 'RETAIL'

    wholesale_custs = sales_svc.list_customers(group='WHOLESALE')
    assert len(wholesale_custs) == 1
    assert wholesale_custs[0]['customer_name'] == 'METRO PHARMA WHOLESALE'
    assert wholesale_custs[0]['total_sales'] == 450000.0
    assert wholesale_custs[0]['current_dues'] == 50000.0

    # Hospital cust had Debit == Credit, so dues should be 0
    hospital_custs = sales_svc.list_customers(group='HOSPITAL')
    assert len(hospital_custs) == 1
    assert hospital_custs[0]['total_sales'] == 300000.0
    assert hospital_custs[0]['current_dues'] == 0.0

    # 4. Test customer summary has group_name
    summary = sales_svc.get_customer_summary(retail_custs[0]['customer_code'])
    assert summary['group_name'] == 'RETAIL'

    # 5. Test purge_customer_data
    purged = ingest_svc.purge_customer_data()
    assert purged['deleted_customers'] == 4
    assert purged['deleted_receivables'] >= 2

    # After purge, customer list must be empty
    remaining = sales_svc.list_customers()
    assert len(remaining) == 0
    groups_after = sales_svc.list_customer_groups()
    assert len(groups_after) == 0

    session.close()

