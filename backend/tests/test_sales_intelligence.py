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
    assert wholesale_custs[0]['current_dues'] == 450000.0       # Debit is Customer Dues
    assert wholesale_custs[0]['company_payable'] == 400000.0    # Credit is Company Liability
    assert wholesale_custs[0]['net_receivable'] == 50000.0      # Net = Debit - Credit
    assert wholesale_custs[0]['total_sales'] == 0.0             # Debit is not sales

    # Hospital cust had Debit 300,000 and Credit 300,000
    hospital_custs = sales_svc.list_customers(group='HOSPITAL')
    assert len(hospital_custs) == 1
    assert hospital_custs[0]['current_dues'] == 300000.0
    assert hospital_custs[0]['company_payable'] == 300000.0
    assert hospital_custs[0]['net_receivable'] == 0.0
    assert hospital_custs[0]['total_sales'] == 0.0

    # 4. Test customer summary has group_name and dues/liability fields
    summary = sales_svc.get_customer_summary(retail_custs[0]['customer_code'])
    assert summary['group_name'] == 'RETAIL'
    assert summary['current_dues'] == 120000.0
    assert summary['company_payable'] == 90000.0
    assert summary['net_receivable'] == 30000.0
    assert summary['total_sales'] == 0.0

    # 5. Test purge_customer_data
    purged = ingest_svc.purge_customer_data()
    assert purged['deleted_customers'] == 4
    assert purged['deleted_receivables'] == 4

    # After purge, customer list must be empty
    remaining = sales_svc.list_customers()
    assert len(remaining) == 0
    groups_after = sales_svc.list_customer_groups()
    session.close()


def test_marg_ledger_588_customers_ingestion_and_group_filtering():
    """Validates complete ingestion and group filtering for 588 MARG ledger rows."""
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    categories = ['RETAIL PHARMACY', 'HOSPITAL SUPPLY', 'WHOLESALE DISTRIBUTOR', 'GOVERNMENT INSTITUTION']
    data = []
    for i in range(1, 589):
        cat = categories[i % len(categories)]
        debit = 10000.0 * (i % 20 + 1)
        credit = 8000.0 * (i % 20 + 1)
        data.append({
            'Ledger': f'CHEMIST STORE {i:03d} PHARMA',
            'Group': cat,
            'Debit': debit,
            'Credit': credit,
            'Balance': debit - credit,
            'Area': f'Zone {i % 10}',
        })

    df = pd.DataFrame(data)
    assert len(df) == 588

    buf = io.BytesIO()
    df.to_excel(buf, index=False)

    ingest_svc = IngestionService(session)
    stats = ingest_svc.ingest_excel(buf.getvalue(), filename='MARG_Ledger_588.xlsx')
    assert stats['customers_upserted'] == 588

    sales_svc = SalesIntelligenceService(session)
    all_custs = sales_svc.list_customers()
    assert len(all_custs) == 588

    groups = sales_svc.list_customer_groups()
    assert set(groups) == set(categories)

    for cat in categories:
        cat_custs = sales_svc.list_customers(group=cat)
        expected_count = sum(1 for d in data if d['Group'] == cat)
        assert len(cat_custs) == expected_count
        for c in cat_custs:
            assert c['group_name'] == cat
            assert c['current_dues'] > 0
            assert c['company_payable'] > 0
            assert c['total_sales'] == 0.0

    session.close()


def test_party_wise_individual_ledger_populates_transactions_and_product_sales():
    """
    Validates the end-to-end multi-tier model:
    1. Customer Master Ledger: Debit populates Customer Dues, Credit populates Company Liable to Pay.
       Other placeholders (total sales, transactions, products) remain 0 / unpopulated.
    2. Party-Wise Individual Ledger: Ingests each transaction, product name, qty, rate, and sales figure
       to populate Total Historical Sales, Order Count, and Purchase History transactions.
    """
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    # Step 1: Upload Customer Master Ledger
    master_df = pd.DataFrame([
        {
            'Ledger': 'ROYAL PHARMA DISTRIBUTORS',
            'Group': 'RETAIL PHARMACY',
            'Debit': 65000.0,
            'Credit': 15000.0,
            'District': 'Kolkata Central',
        }
    ])
    buf_master = io.BytesIO()
    master_df.to_excel(buf_master, index=False)

    ingest_svc = IngestionService(session)
    ingest_svc.ingest_excel(buf_master.getvalue(), filename='Customer_Master_Ledger.xlsx')

    sales_svc = SalesIntelligenceService(session)
    custs = sales_svc.list_customers()
    assert len(custs) == 1
    royal = custs[0]
    assert royal['customer_name'] == 'ROYAL PHARMA DISTRIBUTORS'
    # Only Customer Dues is Debit, and Company Liable to Pay is Credit
    assert royal['current_dues'] == 65000.0
    assert royal['company_payable'] == 15000.0
    assert royal['net_receivable'] == 50000.0
    # Other placeholders are awaiting the party-wise ledger
    assert royal['total_sales'] == 0.0
    assert royal['total_orders'] == 0

    summary_before = sales_svc.get_customer_summary(royal['customer_code'])
    assert summary_before['current_dues'] == 65000.0
    assert summary_before['company_payable'] == 15000.0
    assert summary_before['total_sales'] == 0.0
    assert summary_before['total_orders'] == 0
    assert len(summary_before['recent_orders']) == 0

    # Step 2: Upload Party-Wise Individual Ledger with actual transactions and product sales
    party_ledger_df = pd.DataFrame([
        {
            'Date': '15/05/2026',
            'Party Name': 'ROYAL PHARMA DISTRIBUTORS',
            'Particulars': 'PARACET-650 MG TABLET',
            'Invoice No': 'INV-2026-101',
            'Qty': 100.0,
            'Rate': 20.0,
            'Debit': 2000.0,
        },
        {
            'Date': '28/06/2026',
            'Party Name': 'ROYAL PHARMA DISTRIBUTORS',
            'Particulars': 'AMOXY-500 MG CAPSULE',
            'Invoice No': 'INV-2026-102',
            'Qty': 50.0,
            'Rate': 80.0,
            'Debit': 4000.0,
        },
    ])
    buf_party = io.BytesIO()
    party_ledger_df.to_excel(buf_party, index=False)

    stats = ingest_svc.ingest_excel(buf_party.getvalue(), filename='Royal_Pharma_Party_Ledger.xlsx')
    assert stats['sales_inserted'] == 2

    # Step 3: Verify the UI placeholders are now populated from the party-wise ledger
    summary_after = sales_svc.get_customer_summary(royal['customer_code'])
    # Customer Dues & Liable to Pay remain untouched from Master Ledger
    assert summary_after['current_dues'] == 65000.0
    assert summary_after['company_payable'] == 15000.0
    assert summary_after['net_receivable'] == 50000.0
    # Sales figures and transactions are now populated from Party Ledger
    assert summary_after['total_sales'] == 6000.0
    assert summary_after['total_orders'] == 2
    assert len(summary_after['recent_orders']) == 2

    # Verify transaction details in purchase history
    prod_names = {tx['product_name'] for tx in summary_after['recent_orders']}
    assert 'PARACET-650 MG TABLET' in prod_names
    assert 'AMOXY-500 MG CAPSULE' in prod_names

    session.close()


def test_two_uploaders_master_ledger_and_product_wise_sales_ledger():
    """
    Validates the user's two-uploader workflow in Sales UI:
    Uploader 1: Master Customer Ledger Excel
      - Extracts Ledger (Name), Group (Category), Debit (Customer Dues), Credit (Company Liable to Pay).
      - Purges customer database cleanly when requested.
    Uploader 2: Product-Wise Customer Ledger Excel
      - Ingests customer product sales (Date, Party Name, Particulars/Item, Qty, Rate, Amount).
      - Links to existing customers to populate order history and trigger recommendations.
    """
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.utcnow()

    # 1. Warehouse stock setup: Product MED-001 with near-expiry batch
    p1 = Product(
        product_code='MED-AZITHRO-500',
        product_name='AZITHROMYCIN 500MG TABS',
        category='Antibiotics',
        company='Zydus Cadila',
        unit='strip',
        unit_cost=110.0,
        reorder_enabled=True,
    )
    b1 = InventoryBatch(
        product_code='MED-AZITHRO-500',
        batch_no='ZY-902',
        company='Zydus Cadila',
        qty_on_hand=80.0,
        expiry_date=now + timedelta(days=45),  # 45 days remaining -> near expiry
        unit_cost=110.0,
    )
    session.add_all([p1, b1])
    session.commit()

    ingest_svc = IngestionService(session)
    sales_svc = SalesIntelligenceService(session)

    # ------------------ UPLOADER 1: MASTER CUSTOMER LEDGER ------------------
    master_data = [
        {
            'Ledger': '1ST JULY 2026 - DIGHA',
            'Group': 'RETAIL PHARMACY',
            'Debit': 418067.0,
            'Credit': 0.0,
            'Area': 'Digha Coastal',
        },
        {
            'Ledger': 'PURULIA CARE PHARMA',
            'Group': 'HOSPITAL SUPPLY',
            'Debit': 75000.0,
            'Credit': 15000.0,
            'Area': 'Purulia Central',
        },
    ]
    buf_master = io.BytesIO()
    pd.DataFrame(master_data).to_excel(buf_master, index=False)

    stats_master = ingest_svc.ingest_excel(buf_master.getvalue(), filename='Customer_Master_Ledger.xlsx')
    assert stats_master['customers_upserted'] == 2
    assert stats_master['receivables_upserted'] == 2
    assert stats_master['sales_inserted'] == 0

    # Verify Master Ledger data in UI service
    groups = sales_svc.list_customer_groups()
    assert set(groups) == {'HOSPITAL SUPPLY', 'RETAIL PHARMACY'}

    custs = sales_svc.list_customers()
    assert len(custs) == 2
    digha = next(c for c in custs if c['customer_name'] == '1ST JULY 2026 - DIGHA')
    purulia = next(c for c in custs if c['customer_name'] == 'PURULIA CARE PHARMA')

    assert digha['current_dues'] == 418067.0
    assert digha['company_payable'] == 0.0
    assert digha['total_sales'] == 0.0
    assert digha['total_orders'] == 0

    assert purulia['current_dues'] == 75000.0
    assert purulia['company_payable'] == 15000.0
    assert purulia['net_receivable'] == 60000.0
    assert purulia['total_sales'] == 0.0

    # Verify summary before product ledger
    summary_before = sales_svc.get_customer_summary(digha['customer_code'])
    assert summary_before['total_sales'] == 0.0
    assert summary_before['total_orders'] == 0
    assert len(summary_before['recent_orders']) == 0

    # Recommendations before product ledger: 0 because no order history yet
    recs_before = sales_svc.get_customer_recommendations(digha['customer_code'])
    assert len(recs_before['near_expiry_opportunities']) == 0

    # ------------------ UPLOADER 2: PRODUCT-WISE SALES LEDGER ------------------
    product_ledger_data = [
        {
            'Date': (now - timedelta(days=20)).strftime('%d/%m/%Y'),
            'Party Name': '1ST JULY 2026 - DIGHA',
            'Particulars': 'AZITHROMYCIN 500MG TABS',
            'Invoice No': 'INV-2026-881',
            'Qty': 30.0,
            'Rate': 120.0,
            'Amount': 3600.0,
        },
        {
            'Date': (now - timedelta(days=10)).strftime('%d/%m/%Y'),
            'Party Name': '1ST JULY 2026 - DIGHA',
            'Particulars': 'AZITHROMYCIN 500MG TABS',
            'Invoice No': 'INV-2026-920',
            'Qty': 25.0,
            'Rate': 120.0,
            'Amount': 3000.0,
        },
        {
            'Date': (now - timedelta(days=5)).strftime('%d/%m/%Y'),
            'Party Name': 'PURULIA CARE PHARMA',
            'Particulars': 'PARACETAMOL 650MG',
            'Invoice No': 'INV-2026-955',
            'Qty': 100.0,
            'Rate': 25.0,
            'Amount': 2500.0,
        },
    ]
    buf_prod = io.BytesIO()
    pd.DataFrame(product_ledger_data).to_excel(buf_prod, index=False)

    stats_prod = ingest_svc.ingest_excel(buf_prod.getvalue(), filename='Product_Wise_Sales_Ledger.xlsx')
    assert stats_prod['sales_inserted'] == 3
    assert stats_prod['matched_customers'] == 2

    # Step 3: Verify that Uploader 2 activated Order History and Recommendations!
    summary_after = sales_svc.get_customer_summary(digha['customer_code'])
    assert summary_after['current_dues'] == 418067.0
    assert summary_after['company_payable'] == 0.0
    assert summary_after['total_sales'] == 6600.0  # 3600 + 3000
    assert summary_after['total_orders'] == 2
    assert len(summary_after['recent_orders']) == 2
    assert summary_after['sales_30d'] == 6600.0

    # Near-expiry recommendation is now ACTIVATED for Digha because they buy Azithromycin!
    recs_after = sales_svc.get_customer_recommendations(digha['customer_code'])
    opps = recs_after['near_expiry_opportunities']
    assert len(opps) == 1
    assert opps[0]['product_code'] == 'MED-AZITHRO-500'
    assert opps[0]['batch_no'] == 'ZY-902'
    assert opps[0]['days_remaining'] <= 46
    assert opps[0]['suggested_qty'] > 0

    # Step 4: Verify Purge Customer Database wipes both Master and Sales records
    purged = ingest_svc.purge_customer_data()
    assert purged['deleted_customers'] == 2
    assert purged['deleted_receivables'] == 2
    assert purged['deleted_sales'] == 3

    assert len(sales_svc.list_customers()) == 0
    assert len(sales_svc.list_customer_groups()) == 0

    session.close()


def test_hierarchical_marg_party_product_wise_sales_ledger():
    """
    Validates ingestion of hierarchical MARG exports (e.g., CUSTOMER_PRODUCT LEDGER.XLS)
    where Party names and Products are in the same column, separated by Party Total,
    with page break headers and quantities (Sale Qty, Net Qty).
    """
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    # 1. Setup products with unit cost in catalog
    p1 = Product(
        product_code='MED-ENRON-8',
        product_name='ENRON-8 TAB ALU ALU 10X10',
        category='Tablets',
        company='Ben Remedies',
        unit='box',
        unit_cost=150.0,
        reorder_enabled=True,
    )
    p2 = Product(
        product_code='MED-PETALIFE-D',
        product_name='PETALIFE-D TAB 10x10',
        category='Tablets',
        company='Ben Remedies',
        unit='box',
        unit_cost=80.0,
        reorder_enabled=True,
    )
    session.add_all([p1, p2])
    session.commit()

    ingest_svc = IngestionService(session)
    sales_svc = SalesIntelligenceService(session)

    # Ingest Master Customer Ledger first
    master_df = pd.DataFrame([
        {
            'Ledger': 'A.B. MEDICINE CENTRE-THIRDPARTPURBA BURDWAN',
            'Group': 'RETAIL CHEMIST',
            'Debit': 12500.0,
            'Credit': 500.0,
        }
    ])
    buf_master = io.BytesIO()
    master_df.to_excel(buf_master, index=False)
    ingest_svc.ingest_excel(buf_master.getvalue(), filename='CUSTOMER_MASTER_LEDGER.XLS')

    # 2. Construct hierarchical MARG dataframe simulating CUSTOMER_PRODUCT LEDGER.XLS
    hierarchical_data = [
        {'Party/Product Name': 'Party/Product Wise Net Sales From 01/04/2026 To 03/10/2026', 'Sale Qty': None, 'Ret Qty': None, 'Net Qty': None},
        {'Party/Product Name': 'BEN REMEDIES', 'Sale Qty': None, 'Ret Qty': None, 'Net Qty': None},
        {'Party/Product Name': 'NETAJI SUBHAS ROAD, PURBA BARDHAMAN', 'Sale Qty': None, 'Ret Qty': None, 'Net Qty': None},
        {'Party/Product Name': 'GSTIN : 19AABCB1234F1Z1', 'Sale Qty': None, 'Ret Qty': None, 'Net Qty': None},
        # Customer Header 1
        {'Party/Product Name': 'A.B. MEDICINE CENTRE-THIRDPARTPURBA BURDWAN', 'Sale Qty': None, 'Ret Qty': None, 'Net Qty': None},
        # Product Rows
        {'Party/Product Name': 'ENRON-8 TAB ALU ALU 10X10', 'Sale Qty': 100.0, 'Ret Qty': 0.0, 'Net Qty': 100.0},
        {'Party/Product Name': 'PETALIFE-D TAB 10x10', 'Sale Qty': 50.0, 'Ret Qty': 0.0, 'Net Qty': 50.0},
        # Party Total
        {'Party/Product Name': 'Party Total :', 'Sale Qty': 150.0, 'Ret Qty': 0.0, 'Net Qty': 150.0},
        # Page Break Noise
        {'Party/Product Name': 'Continued..2', 'Sale Qty': None, 'Ret Qty': None, 'Net Qty': None},
        {'Party/Product Name': 'Page No..2', 'Sale Qty': None, 'Ret Qty': None, 'Net Qty': None},
        # Customer Header 2 (New party not in master)
        {'Party/Product Name': 'NEW BARDHAMAN CHEMISTS', 'Sale Qty': None, 'Ret Qty': None, 'Net Qty': None},
        {'Party/Product Name': 'ENRON-8 TAB ALU ALU 10X10', 'Sale Qty': 20.0, 'Ret Qty': 0.0, 'Net Qty': 20.0},
        {'Party/Product Name': 'Party Total :', 'Sale Qty': 20.0, 'Ret Qty': 0.0, 'Net Qty': 20.0},
        {'Party/Product Name': 'Grand Total :', 'Sale Qty': 170.0, 'Ret Qty': 0.0, 'Net Qty': 170.0},
    ]

    buf = io.BytesIO()
    pd.DataFrame(hierarchical_data).to_excel(buf, index=False)

    stats = ingest_svc.ingest_excel(buf.getvalue(), filename='CUSTOMER_PRODUCT_LEDGER.XLS')
    assert stats['sales_inserted'] == 3
    assert stats['matched_customers'] == 2

    # Verify existing customer summary enriched with sales history & products
    cust_ab = next(c for c in sales_svc.list_customers() if 'A.B. MEDICINE' in c['customer_name'])
    summary_ab = sales_svc.get_customer_summary(cust_ab['customer_code'])
    assert summary_ab is not None
    # Master ledger figures must stay intact
    assert summary_ab['current_dues'] == 12500.0
    assert summary_ab['company_payable'] == 500.0
    assert summary_ab['net_receivable'] == 12000.0
    # Sales figures and items must be populated
    assert summary_ab['total_orders'] == 2
    assert summary_ab['total_sales'] == (100 * 150.0) + (50 * 80.0)  # 15,000 + 4,000 = 19,000
    assert len(summary_ab['recent_orders']) == 2

    items = {tx['product_name']: tx['qty'] for tx in summary_ab['recent_orders']}
    assert items['ENRON-8 TAB ALU ALU 10X10'] == 100.0
    assert items['PETALIFE-D TAB 10x10'] == 50.0

    # Verify newly discovered party was auto-created as active customer
    new_cust = next((c for c in sales_svc.list_customers() if 'NEW BARDHAMAN' in c['customer_name']), None)
    assert new_cust is not None
    summary_new = sales_svc.get_customer_summary(new_cust['customer_code'])
    assert summary_new['total_orders'] == 1
    assert summary_new['total_sales'] == 20 * 150.0  # 3000

    session.close()


def test_cross_excel_customer_name_normalization_and_mapping():
    """
    Test that variations in customer names across Master Customer Ledger and
    Product-Wise Customer Ledger are normalized and mapped to the same customer:
    - Normalizes honorifics (M/S), trailing dots, hyphen spacing, and case.
    - Matches canonical keys so both files link to the exact same Customer record.
    - Preserves Customer Dues (Debit) and Company Liable to Pay (Credit).
    - Links Product Purchase History to the single unified customer.
    - Prevents duplicate customer creation.
    """
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    p1 = Product(
        product_code='MED-ENRON-8',
        product_name='ENRON-8 TAB ALU ALU 10X10',
        category='Tablets',
        company='Ben Remedies',
        unit='box',
        unit_cost=150.0,
        reorder_enabled=True,
    )
    p2 = Product(
        product_code='MED-PETALIFE-D',
        product_name='PETALIFE-D TAB 10x10',
        category='Tablets',
        company='Ben Remedies',
        unit='box',
        unit_cost=80.0,
        reorder_enabled=True,
    )
    session.add_all([p1, p2])
    session.commit()

    ingest_svc = IngestionService(session)
    sales_svc = SalesIntelligenceService(session)

    # Master Excel with formatting variations:
    # 1. "A.B. MEDICINE CENTRE-THIRDPARTPURBA BURDWAN" (no hyphen space, merged words)
    # 2. "M/S MAA TARA MEDICAL STORE - BURDWAN ." (honorific M/S, trailing dot)
    master_df = pd.DataFrame([
        {
            'Ledger': 'A.B. MEDICINE CENTRE-THIRDPARTPURBA BURDWAN',
            'Group': 'RETAIL CHEMIST',
            'Debit': 45000.0,
            'Credit': 500.0,
        },
        {
            'Ledger': 'M/S MAA TARA MEDICAL STORE - BURDWAN .',
            'Group': 'RETAIL CHEMIST',
            'Debit': 12000.0,
            'Credit': 0.0,
        }
    ])
    buf_master = io.BytesIO()
    master_df.to_excel(buf_master, index=False)
    ingest_svc.ingest_excel(buf_master.getvalue(), filename='CUSTOMER_MASTER_LEDGER.XLS')

    # Product-wise Excel with different formatting of same customers:
    # 1. "A.B. MEDICINE CENTRE - THIRDPART PURBA BURDWAN" (spaced hyphen & words)
    # 2. "MAA TARA MEDICAL STORE-BURDWAN" (no honorific M/S, no trailing dot, compact hyphen)
    hierarchical_data = [
        {'Party/Product Name': 'Party/Product Wise Net Sales From 01/04/2026 To 03/10/2026', 'Sale Qty': None, 'Ret Qty': None, 'Net Qty': None},
        {'Party/Product Name': 'BEN REMEDIES', 'Sale Qty': None, 'Ret Qty': None, 'Net Qty': None},
        # Party 1: A.B. Medicine Centre
        {'Party/Product Name': 'A.B. MEDICINE CENTRE - THIRDPART PURBA BURDWAN', 'Sale Qty': None, 'Ret Qty': None, 'Net Qty': None},
        {'Party/Product Name': 'ENRON-8 TAB ALU ALU 10X10', 'Sale Qty': 60.0, 'Ret Qty': 0.0, 'Net Qty': 60.0},
        {'Party/Product Name': 'PETALIFE-D TAB 10x10', 'Sale Qty': 40.0, 'Ret Qty': 0.0, 'Net Qty': 40.0},
        {'Party/Product Name': 'Party Total :', 'Sale Qty': 100.0, 'Ret Qty': 0.0, 'Net Qty': 100.0},
        # Party 2: Maa Tara Medical Store
        {'Party/Product Name': 'MAA TARA MEDICAL STORE-BURDWAN', 'Sale Qty': None, 'Ret Qty': None, 'Net Qty': None},
        {'Party/Product Name': 'ENRON-8 TAB ALU ALU 10X10', 'Sale Qty': 25.0, 'Ret Qty': 0.0, 'Net Qty': 25.0},
        {'Party/Product Name': 'Party Total :', 'Sale Qty': 25.0, 'Ret Qty': 0.0, 'Net Qty': 25.0},
    ]
    buf_prod = io.BytesIO()
    pd.DataFrame(hierarchical_data).to_excel(buf_prod, index=False)
    ingest_svc.ingest_excel(buf_prod.getvalue(), filename='CUSTOMER_PRODUCT_LEDGER.XLS')

    # Assert exactly 2 customers exist in database (no duplicates!)
    customers = session.query(Customer).all()
    assert len(customers) == 2, f"Expected 2 unique customers, found {len(customers)}: {[c.customer_name for c in customers]}"

    # Check Customer 1 (A.B. MEDICINE CENTRE)
    cust_ab = next(c for c in customers if 'MEDICINE CENTRE' in c.customer_name)
    summary_ab = sales_svc.get_customer_summary(cust_ab.customer_code)
    assert summary_ab['current_dues'] == 45000.0
    assert summary_ab['company_payable'] == 500.0
    assert summary_ab['net_receivable'] == 44500.0
    assert summary_ab['total_orders'] == 2
    assert summary_ab['total_sales'] == (60 * 150.0) + (40 * 80.0)
    assert len(summary_ab['recent_orders']) == 2

    # Check Customer 2 (MAA TARA MEDICAL STORE)
    cust_tara = next(c for c in customers if 'MAA TARA' in c.customer_name)
    summary_tara = sales_svc.get_customer_summary(cust_tara.customer_code)
    assert summary_tara['current_dues'] == 12000.0
    assert summary_tara['company_payable'] == 0.0
    assert summary_tara['total_orders'] == 1
    assert summary_tara['total_sales'] == (25 * 150.0)
    assert len(summary_tara['recent_orders']) == 1

    session.close()


def test_cross_excel_reverse_upload_order_mapping():
    """
    Test that when Product-Wise Customer Ledger is uploaded FIRST,
    and Master Customer Ledger is uploaded SECOND, customer mapping still succeeds:
    - Products and sales history attach to the customer.
    - Master ledger enriches the same customer with Group, Dues (Debit), and Credit.
    - No duplicate customer records are created.
    """
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    p1 = Product(
        product_code='MED-ENRON-8',
        product_name='ENRON-8 TAB ALU ALU 10X10',
        category='Tablets',
        company='Ben Remedies',
        unit='box',
        unit_cost=150.0,
        reorder_enabled=True,
    )
    session.add(p1)
    session.commit()

    ingest_svc = IngestionService(session)
    sales_svc = SalesIntelligenceService(session)

    # 1. Product ledger uploaded first
    hierarchical_data = [
        {'Party/Product Name': 'Party/Product Wise Net Sales From 01/04/2026 To 03/10/2026', 'Sale Qty': None, 'Ret Qty': None, 'Net Qty': None},
        {'Party/Product Name': 'A.B. MEDICINE CENTRE - THIRDPART PURBA BURDWAN', 'Sale Qty': None, 'Ret Qty': None, 'Net Qty': None},
        {'Party/Product Name': 'ENRON-8 TAB ALU ALU 10X10', 'Sale Qty': 50.0, 'Ret Qty': 0.0, 'Net Qty': 50.0},
        {'Party/Product Name': 'Party Total :', 'Sale Qty': 50.0, 'Ret Qty': 0.0, 'Net Qty': 50.0},
    ]
    buf_prod = io.BytesIO()
    pd.DataFrame(hierarchical_data).to_excel(buf_prod, index=False)
    ingest_svc.ingest_excel(buf_prod.getvalue(), filename='CUSTOMER_PRODUCT_LEDGER.XLS')

    # 2. Master ledger uploaded second with name variation
    master_df = pd.DataFrame([
        {
            'Ledger': 'A.B. MEDICINE CENTRE-THIRDPARTPURBA BURDWAN',
            'Group': 'RETAIL CHEMIST',
            'Debit': 38000.0,
            'Credit': 200.0,
        }
    ])
    buf_master = io.BytesIO()
    master_df.to_excel(buf_master, index=False)
    ingest_svc.ingest_excel(buf_master.getvalue(), filename='CUSTOMER_MASTER_LEDGER.XLS')

    # Check database: exactly 1 customer
    customers = session.query(Customer).all()
    assert len(customers) == 1, f"Expected 1 unique customer, found {len(customers)}"

    cust = customers[0]
    assert cust.group_name == 'RETAIL CHEMIST'

    summary = sales_svc.get_customer_summary(cust.customer_code)
    assert summary['current_dues'] == 38000.0
    assert summary['company_payable'] == 200.0
    assert summary['net_receivable'] == 37800.0
    assert summary['total_orders'] == 1
    assert summary['total_sales'] == 50 * 150.0

    session.close()


def test_purchase_history_transacted_products_from_product_wise_ledger():
    """
    Test that the Purchase History tab / customer summary provides the complete
    transacted product portfolio extracted from the Product-Wise Customer Ledger Excel:
    - Lists each unique product transacted by the customer.
    - Aggregates total quantities purchased, order counts, and values.
    - Preserves detailed line-by-line transactions.
    """
    from backend.app.main import get_customer_transacted_products

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    # 1. Product catalog
    p1 = Product(product_code='MED-AMX500', product_name='AMX-500 TAB 10X10', company='Cipla', category='Antibiotics', unit_cost=50.0, reorder_enabled=True)
    p2 = Product(product_code='MED-PCM650', product_name='PCM-650 TAB 10X10', company='Apex', category='Analgesics', unit_cost=25.0, reorder_enabled=True)
    session.add_all([p1, p2])
    session.commit()

    ingest_svc = IngestionService(session)
    sales_svc = SalesIntelligenceService(session)

    # 2. Ingest Master Customer Ledger
    master_df = pd.DataFrame([
        {
            'Ledger': 'APEX PHARMACY - DURGAPUR',
            'Group': 'RETAIL CHEMIST',
            'Debit': 25000.0,
            'Credit': 1000.0,
        }
    ])
    buf_m = io.BytesIO()
    master_df.to_excel(buf_m, index=False)
    ingest_svc.ingest_excel(buf_m.getvalue(), filename='CUSTOMER_MASTER_LEDGER.XLS')

    # 3. Ingest Product-Wise Customer Ledger
    hierarchical_data = [
        {'Party/Product Name': 'Party/Product Wise Net Sales From 01/04/2026 To 03/10/2026', 'Sale Qty': None, 'Ret Qty': None, 'Net Qty': None},
        {'Party/Product Name': 'BEN REMEDIES', 'Sale Qty': None, 'Ret Qty': None, 'Net Qty': None},
        {'Party/Product Name': 'APEX PHARMACY - DURGAPUR', 'Sale Qty': None, 'Ret Qty': None, 'Net Qty': None},
        {'Party/Product Name': 'AMX-500 TAB 10X10', 'Sale Qty': 100.0, 'Ret Qty': 0.0, 'Net Qty': 100.0},
        {'Party/Product Name': 'PCM-650 TAB 10X10', 'Sale Qty': 200.0, 'Ret Qty': 0.0, 'Net Qty': 200.0},
        {'Party/Product Name': 'AMX-500 TAB 10X10', 'Sale Qty': 50.0, 'Ret Qty': 0.0, 'Net Qty': 50.0},
        {'Party/Product Name': 'Party Total :', 'Sale Qty': 350.0, 'Ret Qty': 0.0, 'Net Qty': 350.0},
    ]
    buf_p = io.BytesIO()
    pd.DataFrame(hierarchical_data).to_excel(buf_p, index=False)
    ingest_svc.ingest_excel(buf_p.getvalue(), filename='CUSTOMER_PRODUCT_LEDGER.XLS')

    # 4. Verify Customer Summary
    cust = session.query(Customer).first()
    assert cust is not None
    summary = sales_svc.get_customer_summary(cust.customer_code)

    assert 'transacted_products' in summary
    t_prods = summary['transacted_products']
    assert len(t_prods) == 2, f"Expected 2 transacted products, got: {t_prods}"

    # Map by product name
    prod_map = {p['product_name']: p for p in t_prods}
    assert 'AMX-500 TAB 10X10' in prod_map
    assert 'PCM-650 TAB 10X10' in prod_map

    amx = prod_map['AMX-500 TAB 10X10']
    assert amx['total_qty'] == 150.0
    assert amx['order_count'] == 2
    assert amx['total_amount'] == 150.0 * 50.0  # 7500.0
    assert amx['company'] == 'Cipla'

    pcm = prod_map['PCM-650 TAB 10X10']
    assert pcm['total_qty'] == 200.0
    assert pcm['order_count'] == 1
    assert pcm['total_amount'] == 200.0 * 25.0  # 5000.0
    assert pcm['company'] == 'Apex'

    # Check recent orders transaction log
    assert len(summary['recent_orders']) == 3

    # 5. Verify route get_customer_transacted_products
    api_prods = get_customer_transacted_products(cust.customer_code, db=session)
    assert len(api_prods) == 2
    assert {p['product_name'] for p in api_prods} == {'AMX-500 TAB 10X10', 'PCM-650 TAB 10X10'}

    session.close()


def test_product_wise_customer_ledger_exact_user_scenario():
    """
    Validates the exact scenario requested by the user:
    - Master Customer Ledger has: 'A.B.MEDICINE CENTRE           KATWA'
      with Debit (Customer Dues) = 15420.50 and Credit (Company Liability) = 350.00
    - Product-Wise Customer Ledger has pre-header banner: 'A.B.MEDICINE CENTRE-KATWA'
      with table columns: 'D E S C R I P T I O N', 'QTY.', 'FREE', 'RATE', 'AMOUNT'
      and bottom row: 'Total'
    - Normalization maps both to the exact same Customer record (CUST-ABMEDICINECENTREKATWA).
    - Customer Dues (Debit) and Company Liability (Credit) are preserved.
    - Purchase history transacted products table contains description, qty, free, rate, amount and exact totals.
    """
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    ingest_svc = IngestionService(session)
    sales_svc = SalesIntelligenceService(session)

    # 1. Ingest Master Customer Ledger
    master_df = pd.DataFrame([
        {
            'Ledger': 'A.B.MEDICINE CENTRE           KATWA',
            'Group': 'RETAIL CHEMIST',
            'Debit': 15420.50,
            'Credit': 350.00,
        }
    ])
    buf_m = io.BytesIO()
    master_df.to_excel(buf_m, index=False)
    ingest_svc.ingest_excel(buf_m.getvalue(), filename='CUSTOMER_MASTER_LEDGER.XLS')

    # 2. Ingest Product-Wise Customer Ledger matching MARG export layout
    # Row 0: Company name
    # Row 1: Company address
    # Row 2: Customer Name (A.B.MEDICINE CENTRE-KATWA)
    # Row 3: Date range
    # Row 4: Header row
    # Row 5+: Data rows
    # Last Row: Total row
    raw_excel_rows = [
        ['BEN REMEDIES', None, None, None, None],
        ['NETAJI SUBHAS ROAD, PURBA BARDHAMAN', None, None, None, None],
        ['A.B.MEDICINE CENTRE-KATWA', None, None, None, None],
        ['From 01/04/2026 to 03/10/2026', None, None, None, None],
        ['D E S C R I P T I O N', 'QTY.', 'FREE', 'RATE', 'AMOUNT'],
        ['CEFTAZ 1 GM INJ.', 100.0, 10.0, 250.0, 25000.0],
        ['AMIKACIN 500 MG INJ.', 200.0, 0.0, 65.0, 13000.0],
        ['Total', 300.0, 10.0, None, 38000.0],
    ]
    df_product_ledger = pd.DataFrame(raw_excel_rows)
    buf_p = io.BytesIO()
    df_product_ledger.to_excel(buf_p, header=False, index=False)
    ingest_svc.ingest_excel(buf_p.getvalue(), filename='PRODUCT_WISE_CUSTOMER_LEDGER.XLS')

    # 3. Verify single customer created and resolved
    custs = session.query(Customer).all()
    assert len(custs) == 1, f"Expected exactly 1 customer, found {len(custs)}: {[c.customer_name for c in custs]}"
    cust = custs[0]
    assert cust.customer_code == 'CUST-ABMEDICINECENTREKATWA'

    # 4. Verify Customer Summary
    summary = sales_svc.get_customer_summary(cust.customer_code)

    # Financial Dues & Liabilities
    assert summary['current_dues'] == 15420.50
    assert summary['company_payable'] == 350.00
    assert summary['net_receivable'] == 15420.50 - 350.00

    # Purchase History Transacted Products
    assert 'transacted_products' in summary
    t_prods = summary['transacted_products']
    assert len(t_prods) == 2, f"Expected 2 products, got: {t_prods}"

    prod_map = {p['description']: p for p in t_prods}
    assert 'CEFTAZ 1 GM INJ.' in prod_map
    assert 'AMIKACIN 500 MG INJ.' in prod_map

    ceftaz = prod_map['CEFTAZ 1 GM INJ.']
    assert ceftaz['qty'] == 100.0
    assert ceftaz['free'] == 10.0
    assert ceftaz['rate'] == 250.0
    assert ceftaz['amount'] == 25000.0

    amikacin = prod_map['AMIKACIN 500 MG INJ.']
    assert amikacin['qty'] == 200.0
    assert amikacin['free'] == 0.0
    assert amikacin['rate'] == 65.0
    assert amikacin['amount'] == 13000.0

    # Summary Totals
    assert 'product_totals' in summary
    totals = summary['product_totals']
    assert totals['total_qty'] == 300.0
    assert totals['total_free'] == 10.0
    assert totals['total_amount'] == 38000.0

    # Verify 'Total' footer was not inserted as a product
    all_sh = session.query(SalesHistory).all()
    assert not any('TOTAL' in s.product_name.upper() for s in all_sh)

    session.close()


def test_product_wise_customer_ledger_reverse_order_ingestion():
    """
    Validates that uploading Product-Wise Customer Ledger FIRST and Master Customer Ledger SECOND
    also successfully unifies the customer and preserves both financial dues and purchase history.
    """
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    ingest_svc = IngestionService(session)
    sales_svc = SalesIntelligenceService(session)

    # 1. Ingest Product-Wise Customer Ledger FIRST
    raw_excel_rows = [
        ['BEN REMEDIES', None, None, None, None],
        ['NETAJI SUBHAS ROAD, PURBA BARDHAMAN', None, None, None, None],
        ['A.B.MEDICINE CENTRE-KATWA', None, None, None, None],
        ['From 01/04/2026 to 03/10/2026', None, None, None, None],
        ['D E S C R I P T I O N', 'QTY.', 'FREE', 'RATE', 'AMOUNT'],
        ['CEFTAZ 1 GM INJ.', 50.0, 5.0, 250.0, 12500.0],
    ]
    df_product_ledger = pd.DataFrame(raw_excel_rows)
    buf_p = io.BytesIO()
    df_product_ledger.to_excel(buf_p, header=False, index=False)
    ingest_svc.ingest_excel(buf_p.getvalue(), filename='PRODUCT_WISE_CUSTOMER_LEDGER.XLS')

    # 2. Ingest Master Customer Ledger SECOND
    master_df = pd.DataFrame([
        {
            'Ledger': 'A.B.MEDICINE CENTRE           KATWA',
            'Group': 'RETAIL CHEMIST',
            'Debit': 8200.00,
            'Credit': 150.00,
        }
    ])
    buf_m = io.BytesIO()
    master_df.to_excel(buf_m, index=False)
    ingest_svc.ingest_excel(buf_m.getvalue(), filename='CUSTOMER_MASTER_LEDGER.XLS')

    # 3. Verify unified customer
    custs = session.query(Customer).all()
    assert len(custs) == 1
    cust = custs[0]
    assert cust.customer_code == 'CUST-ABMEDICINECENTREKATWA'

    summary = sales_svc.get_customer_summary(cust.customer_code)
    assert summary['current_dues'] == 8200.00
    assert summary['company_payable'] == 150.00
    assert len(summary['transacted_products']) == 1
    assert summary['transacted_products'][0]['qty'] == 50.0
    assert summary['transacted_products'][0]['free'] == 5.0
    assert summary['transacted_products'][0]['amount'] == 12500.0

    session.close()


def test_hierarchical_party_item_wise_sales_summary_multi_customer():
    """
    Validates parsing and ingestion of multi-customer hierarchical report
    like MARG 'PARTY / ITEM WISE SALES SUMMARY' (LEDGER_PRODUCT_PARTY.XLS).
    Ensures that each customer section is isolated and mapped to the right customer
    rather than all products being attributed to a banner string like 'REPORT FOR : SALE-S/R'.
    """
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    ingest_svc = IngestionService(session)
    sales_svc = SalesIntelligenceService(session)

    # 1. First ingest master customer ledger
    master_df = pd.DataFrame([
        {
            'Ledger': 'A.B.MEDICINE CENTRE           KATWA',
            'Group': 'RETAIL CHEMIST',
            'Debit': 55000.00,
            'Credit': 0.00,
        },
        {
            'Ledger': 'A.B. MEDICINE CENTRE-THIRDPART-PURBA BURDWAN',
            'Group': 'RETAIL CHEMIST',
            'Debit': 12000.00,
            'Credit': 500.00,
        }
    ])
    buf_m = io.BytesIO()
    master_df.to_excel(buf_m, index=False)
    ingest_svc.ingest_excel(buf_m.getvalue(), filename='LEDGER.XLS')

    # 2. Ingest hierarchical party/item report (simulating LEDGER_PRODUCT_PARTY.XLS)
    raw_excel_rows = [
        ['BEN REMEDIES', None, None, None, None, None],
        ['NETAJI SUBHAS ROAD', None, None, None, None, None],
        ['PARTY / ITEM WISE SALES SUMMARY FROM 01/04/2026-07/10/2026', None, None, None, None, None],
        ['Report For : SALE-S/R', None, None, None, None, None],
        [None, None, None, None, None, None],
        ['D E S C R I P T I O N', 'QTY.', 'FREE', 'RATE', 'AMOUNT', '( % )'],
        ['--------------------------------------------------------------------------------------------------', None, None, None, None, None],
        ['A.B. MEDICINE CENTRE-THIRDPART-PURBA BURDWAN', None, None, None, None, None],
        ['CEFTAZ 1 GM INJ.', 20.0, 2.0, 250.0, 5000.0, 10.0],
        [' TOTAL :', 20.0, 2.0, None, 5000.0, None],
        ['--------------------------------------------------------------------------------------------------', None, None, None, None, None],
        ['A.B.MEDICINE CENTRE-KATWA', None, None, None, None, None],
        ['AZITHROMYCIN 500MG TABS', 100.0, 10.0, 110.0, 11000.0, 15.0],
        ['PAN-D CAPSULE', 50.0, 5.0, 80.0, 4000.0, 5.0],
        [' TOTAL :', 150.0, 15.0, None, 15000.0, None],
    ]
    df_item_summary = pd.DataFrame(raw_excel_rows)
    buf_i = io.BytesIO()
    df_item_summary.to_excel(buf_i, header=False, index=False)
    result = ingest_svc.ingest_excel(buf_i.getvalue(), filename='LEDGER_PRODUCT_PARTY.XLS')

    assert result['sales_inserted'] == 3

    # 3. Check customer A.B.MEDICINE CENTRE-KATWA
    cust_katwa = session.query(Customer).filter_by(customer_code='CUST-ABMEDICINECENTREKATWA').first()
    assert cust_katwa is not None
    summary_katwa = sales_svc.get_customer_summary(cust_katwa.customer_code)

    assert summary_katwa['current_dues'] == 55000.00
    assert len(summary_katwa['transacted_products']) == 2
    prods_katwa = {p['description']: p for p in summary_katwa['transacted_products']}
    assert 'AZITHROMYCIN 500MG TABS' in prods_katwa
    assert prods_katwa['AZITHROMYCIN 500MG TABS']['qty'] == 100.0
    assert prods_katwa['AZITHROMYCIN 500MG TABS']['free'] == 10.0
    assert prods_katwa['AZITHROMYCIN 500MG TABS']['amount'] == 11000.0

    assert 'PAN-D CAPSULE' in prods_katwa
    assert prods_katwa['PAN-D CAPSULE']['qty'] == 50.0
    assert prods_katwa['PAN-D CAPSULE']['free'] == 5.0
    assert prods_katwa['PAN-D CAPSULE']['amount'] == 4000.0

    totals_katwa = summary_katwa['product_totals']
    assert totals_katwa['total_qty'] == 150.0
    assert totals_katwa['total_free'] == 15.0
    assert totals_katwa['total_amount'] == 15000.0

    # 4. Check customer A.B. MEDICINE CENTRE-THIRDPART-PURBA BURDWAN
    cust_burdwan = session.query(Customer).filter_by(customer_code='CUST-ABMEDICINECENTRETHIRDPARTPURBABURDWAN').first()
    assert cust_burdwan is not None
    summary_burdwan = sales_svc.get_customer_summary(cust_burdwan.customer_code)
    assert len(summary_burdwan['transacted_products']) == 1
    assert summary_burdwan['transacted_products'][0]['description'] == 'CEFTAZ 1 GM INJ.'
    assert summary_burdwan['transacted_products'][0]['qty'] == 20.0

    # 5. Verify NO dummy customer code like CUST-REPORTFORSALESR was created
    dummy_cust = session.query(Customer).filter_by(customer_code='CUST-REPORTFORSALESR').first()
    assert dummy_cust is None

    session.close()


def test_sales_dashboard_overview_and_revenue_bins():
    """
    Validates get_sales_dashboard_overview:
    - Total number of customers
    - Total sales
    - Categorization into revenue bins:
      [0 to 2 Lakh, >2 to 5 Lakh, >5 to 8 Lakh, >8 to 12 Lakh, >12 to 20 Lakh, >20 Lakh]
    """
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    # Create 6 customers in different revenue brackets
    customers_data = [
        ('CUST-1', 'MEDICINE SHOP 1', 'General', 50000.0),       # 0 to 2L
        ('CUST-2', 'MEDICINE SHOP 2', 'General', 300000.0),      # >2 to 5L
        ('CUST-3', 'MEDICINE SHOP 3', 'General', 650000.0),      # >5 to 8L
        ('CUST-4', 'MEDICINE SHOP 4', 'General', 1000000.0),     # >8 to 12L
        ('CUST-5', 'MEDICINE SHOP 5', 'General', 1500000.0),     # >12 to 20L
        ('CUST-6', 'MEDICINE SHOP 6', 'General', 3500000.0),     # >20L
    ]

    for code, name, grp, sales_amt in customers_data:
        c = Customer(
            customer_code=code,
            customer_name=name,
            group_name=grp,
            district='Purba Bardhaman',
            status='ACTIVE'
        )
        session.add(c)
        # Add sales history row
        sh = SalesHistory(
            product_code='MED-001',
            product_name='AZITHROMYCIN 500',
            sale_date=datetime.utcnow(),
            qty_sold=10.0,
            rate=sales_amt / 10.0,
            amount=sales_amt,
            customer_code=code,
            customer_name=name
        )
        session.add(sh)

    session.commit()

    sales_svc = SalesIntelligenceService(session)
    overview = sales_svc.get_sales_dashboard_overview()

    assert overview['total_customers'] == 6
    assert overview['active_customers'] == 6
    assert overview['total_sales'] == 50000.0 + 300000.0 + 650000.0 + 1000000.0 + 1500000.0 + 3500000.0

    bins = {b['bin_label']: b for b in overview['customer_sales_bins']}
    assert '0 to 2 Lakh' in bins
    assert bins['0 to 2 Lakh']['customer_count'] == 1
    assert bins['0 to 2 Lakh']['total_sales'] == 50000.0

    assert '>2 to 5 Lakh' in bins
    assert bins['>2 to 5 Lakh']['customer_count'] == 1
    assert bins['>2 to 5 Lakh']['total_sales'] == 300000.0

    assert '>5 to 8 Lakh' in bins
    assert bins['>5 to 8 Lakh']['customer_count'] == 1
    assert bins['>5 to 8 Lakh']['total_sales'] == 650000.0

    assert '>8 to 12 Lakh' in bins
    assert bins['>8 to 12 Lakh']['customer_count'] == 1
    assert bins['>8 to 12 Lakh']['total_sales'] == 1000000.0

    assert '>12 to 20 Lakh' in bins
    assert bins['>12 to 20 Lakh']['customer_count'] == 1
    assert bins['>12 to 20 Lakh']['total_sales'] == 1500000.0

    assert '>20 Lakh' in bins
    assert bins['>20 Lakh']['customer_count'] == 1
    assert bins['>20 Lakh']['total_sales'] == 3500000.0

    session.close()


def test_sales_dashboard_overview_group_filtering():
    """
    Validates that get_sales_dashboard_overview dynamically recalculates all
    sales KPIs, customer counts, and revenue slabs when a customer category/group is specified.
    """
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    # Create 3 customers in SUNDRY DEBTORS and 2 in HOSPITAL SUPPLY
    custs = [
        ('CUST-D1', 'DEBTOR CHEMIST 1', 'SUNDRY DEBTORS', 100000.0),
        ('CUST-D2', 'DEBTOR CHEMIST 2', 'SUNDRY DEBTORS', 400000.0),
        ('CUST-D3', 'DEBTOR CHEMIST 3', 'SUNDRY DEBTORS', 700000.0),
        ('CUST-H1', 'HOSPITAL 1', 'HOSPITAL SUPPLY', 1500000.0),
        ('CUST-H2', 'HOSPITAL 2', 'HOSPITAL SUPPLY', 3000000.0),
    ]

    for code, name, grp, sales_amt in custs:
        session.add(Customer(
            customer_code=code,
            customer_name=name,
            group_name=grp,
            district='Purba Bardhaman',
            status='ACTIVE'
        ))
        session.add(SalesHistory(
            product_code='MED-001',
            product_name='AZITHROMYCIN 500',
            sale_date=datetime.utcnow(),
            qty_sold=10.0,
            rate=sales_amt / 10.0,
            amount=sales_amt,
            customer_code=code,
            customer_name=name
        ))

    session.commit()
    sales_svc = SalesIntelligenceService(session)

    # 1. Overview for ALL categories
    all_overview = sales_svc.get_sales_dashboard_overview()
    assert all_overview['total_customers'] == 5
    assert all_overview['total_sales'] == 100000.0 + 400000.0 + 700000.0 + 1500000.0 + 3000000.0

    # 2. Overview filtered specifically by 'SUNDRY DEBTORS'
    debtor_overview = sales_svc.get_sales_dashboard_overview(group='SUNDRY DEBTORS')
    assert debtor_overview['total_customers'] == 3
    assert debtor_overview['total_sales'] == 100000.0 + 400000.0 + 700000.0

    debtor_bins = {b['bin_label']: b for b in debtor_overview['customer_sales_bins']}
    assert debtor_bins['0 to 2 Lakh']['customer_count'] == 1
    assert debtor_bins['0 to 2 Lakh']['total_sales'] == 100000.0

    assert debtor_bins['>2 to 5 Lakh']['customer_count'] == 1
    assert debtor_bins['>2 to 5 Lakh']['total_sales'] == 400000.0

    assert debtor_bins['>5 to 8 Lakh']['customer_count'] == 1
    assert debtor_bins['>5 to 8 Lakh']['total_sales'] == 700000.0

    assert debtor_bins['>8 to 12 Lakh']['customer_count'] == 0
    assert debtor_bins['>12 to 20 Lakh']['customer_count'] == 0
    assert debtor_bins['>20 Lakh']['customer_count'] == 0

    # 3. Overview filtered specifically by 'HOSPITAL SUPPLY'
    hosp_overview = sales_svc.get_sales_dashboard_overview(group='HOSPITAL SUPPLY')
    assert hosp_overview['total_customers'] == 2
    assert hosp_overview['total_sales'] == 1500000.0 + 3000000.0

    hosp_bins = {b['bin_label']: b for b in hosp_overview['customer_sales_bins']}
    assert hosp_bins['0 to 2 Lakh']['customer_count'] == 0
    assert hosp_bins['>12 to 20 Lakh']['customer_count'] == 1
    assert hosp_bins['>12 to 20 Lakh']['total_sales'] == 1500000.0
    assert hosp_bins['>20 Lakh']['customer_count'] == 1
    assert hosp_bins['>20 Lakh']['total_sales'] == 3000000.0

    session.close()


def test_sales_dashboard_overview_non_active_buyers_with_dues_and_zero_dues():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Create 3 debtors:
    # 1. Active: sales > 0, dues > 0
    # 2. Non-Active with >0 Dues: sales == 0, dues > 0
    # 3. Non-Active with 0 Dues: sales == 0, dues == 0
    c1 = Customer(customer_code='CUST-A1', customer_name='ACTIVE PHARMA', group_name='SUNDRY DEBTORS', district='Kolkata', status='ACTIVE')
    c2 = Customer(customer_code='CUST-NA1', customer_name='DORMANT DEBTOR', group_name='SUNDRY DEBTORS', district='Kolkata', status='ACTIVE')
    c3 = Customer(customer_code='CUST-NA2', customer_name='SETTLED DEBTOR', group_name='SUNDRY DEBTORS', district='Kolkata', status='ACTIVE')
    session.add_all([c1, c2, c3])

    # Invoices / Ledger Dues
    session.add(CustomerReceivable(customer_code='CUST-A1', invoice_no='INV-01', outstanding_amount=5000.0, days_due=10))
    session.add(CustomerReceivable(customer_code='CUST-NA1', invoice_no='INV-02', outstanding_amount=15000.0, days_due=60))
    # CUST-NA2 has no outstanding invoice (dues = 0.0)

    # Sales transactions only for CUST-A1
    session.add(SalesHistory(
        product_code='MED-001',
        product_name='AZITHROMYCIN 500',
        sale_date=datetime.utcnow(),
        qty_sold=10.0,
        rate=100.0,
        amount=1000.0,
        customer_code='CUST-A1',
        customer_name='ACTIVE PHARMA'
    ))
    session.commit()

    sales_svc = SalesIntelligenceService(session)
    overview = sales_svc.get_sales_dashboard_overview(group='SUNDRY DEBTORS')

    assert overview['total_customers'] == 3
    assert overview['active_customers'] == 1
    assert overview['inactive_customers'] == 2
    assert overview['non_active_with_dues'] == 1
    assert overview['non_active_with_dues_amount'] == 15000.0
    assert overview['non_active_zero_dues'] == 1
    assert overview['total_sales'] == 1000.0
    assert overview['total_dues'] == 20000.0

    session.close()




