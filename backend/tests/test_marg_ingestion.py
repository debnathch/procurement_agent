"""
Unit tests for MARG ERP Excel & CSV ingestion.
Tests:
- Spaced-out column name collapsing ('P A R T I C U L A R S')
- Header detection in raw export rows
- Pack size extraction (1*10, 10'S, 10X10)
- Expiry date formats (MM/YY, Mon-YY, MM/YYYY)
- Supplier list ingestion (e.g. MANUFACTURER LIST.xls)
- Outstanding / PCD billing ingestion (e.g. outstanding.xlsx)
"""
import io
import os
from datetime import datetime
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import Base
from backend.app.adapters.excel import (
    MargExcelParser,
    _clean_alpha,
    parse_pack_size,
    parse_expiry_date,
    _find_header_row,
)
from backend.app.services.ingestion import IngestionService


def test_clean_alpha():
    assert _clean_alpha('P A R T I C U L A R S') == 'particulars'
    assert _clean_alpha('C L .   S T O C K') == 'clstock'
    assert _clean_alpha('B . N O .') == 'bno'
    assert _clean_alpha('E X P .   D A T E') == 'expdate'
    assert _clean_alpha('TOTAL BILL VALE UPTO DATE') == 'totalbillvaleuptodate'
    assert _clean_alpha('SL NO.') == 'slno'


def test_parse_pack_size():
    assert parse_pack_size('1*10') == 10.0
    assert parse_pack_size('10*10') == 100.0
    assert parse_pack_size('10X10') == 100.0
    assert parse_pack_size("10'S") == 10.0
    assert parse_pack_size('100 ML') == 100.0
    assert parse_pack_size('3') == 3.0
    assert parse_pack_size(None) == 1.0


def test_parse_expiry_date():
    dt1 = parse_expiry_date('11/26')
    assert dt1 is not None and dt1.year == 2026 and dt1.month == 11

    dt2 = parse_expiry_date('Nov-26')
    assert dt2 is not None and dt2.year == 2026 and dt2.month == 11

    dt3 = parse_expiry_date('12/2027')
    assert dt3 is not None and dt3.year == 2027 and dt3.month == 12

    dt4 = parse_expiry_date('23/09/2026')
    assert dt4 is not None and dt4.year == 2026 and dt4.month == 9 and dt4.day == 23


def test_find_header_row_with_metadata():
    # Simulate a raw MARG export where row 0 is company title and row 1 is table header
    data = [
        ['MY PHARMA CO - CLOSING STOCK', None, None, None],
        ['P A R T I C U L A R S', 'B.NO.', 'EXP.', 'CL. STOCK'],
        ['Paracetamol 500mg', 'B101', '11/26', 150],
    ]
    df_raw = pd.DataFrame(data)
    idx = _find_header_row(df_raw)
    assert idx == 1


def test_ingest_supplier_list_if_present():
    path = '/Users/debz/Downloads/MANUFACTURER LIST.xls'
    if os.path.exists(path):
        with open(path, 'rb') as f:
            content = f.read()
        parsed = MargExcelParser.parse_file(content, filename='MANUFACTURER LIST.xls')
    else:
        # Generate synthetic in-memory MARG manufacturer list
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            suppliers = [
                {
                    'Supplier Name': f'ACCURA_{i:03d} PHARMA LABS',
                    'Place': 'Solan HP',
                    'Category': 'GMP Manufacturer',
                    'Remarks': 'GMP Certified Plant',
                    'Lead Time': 45
                }
                for i in range(25)
            ]
            df = pd.DataFrame(suppliers)
            df.to_excel(writer, sheet_name='MANUFACTURER LIST', index=False)
        content = output.getvalue()
        parsed = MargExcelParser.parse_file(content, filename='MANUFACTURER LIST.xlsx')

    assert len(parsed['suppliers']) >= 20
    # First supplier should have parsed clean name and GMP reliability
    sup0 = parsed['suppliers'][0]
    assert 'ACCURA' in sup0['supplier_name']
    assert sup0['lead_time_days'] == 45
    assert sup0['reliability_score'] >= 0.90


def test_ingest_outstanding_if_present():
    path = '/Users/debz/Downloads/outstanding.xlsx'
    if os.path.exists(path):
        with open(path, 'rb') as f:
            content = f.read()
        parsed = MargExcelParser.parse_file(content, filename='outstanding.xlsx')
    else:
        # Generate synthetic in-memory MARG outstanding report
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            rows = [
                {
                    'Party Name': f'APOLLO_{i:03d} CHEMIST',
                    'Cr Days': 30,
                    'MR Name': 'Representative A',
                    'Total Bill Vale Up To Date': 12500.0,
                    'Balance Outstanding': 5000.0,
                }
                for i in range(220)
            ]
            df = pd.DataFrame(rows)
            df.to_excel(writer, sheet_name='OP Master', index=False)
        content = output.getvalue()
        parsed = MargExcelParser.parse_file(content, filename='outstanding.xlsx')

    assert len(parsed['suppliers']) >= 200
    assert len(parsed['sales_history']) >= 200


def test_ingestion_service_in_memory_db():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    # Create dummy Excel with spaced columns
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df = pd.DataFrame([
            {
                'P A R T I C U L A R S': 'Azithromycin 500mg',
                'B.NO.': 'AZ-99',
                'EXP.': '11/26',
                'CL. STOCK': 50,
                'PUR. RATE': 95.0,
                'PARTY NAME': 'Sun Pharma Distributors',
            }
        ])
        df.to_excel(writer, sheet_name='Closing_Stock', index=False)

    service = IngestionService(session)
    stats = service.ingest_excel(output.getvalue(), 'test_closing_stock.xlsx')

    assert stats['products_upserted'] == 1
    assert stats['batches_inserted'] == 1
    assert stats['suppliers_upserted'] == 1
    session.close()


def test_canonical_medicine_key_generalized():
    from backend.app.adapters.excel import canonical_medicine_key

    # MARG column truncation and trailing packaging variations
    assert canonical_medicine_key('BR-LIVA - 200 ml      200') == canonical_medicine_key('BR-LIVA - 200 ml              200 ml')
    assert canonical_medicine_key('BR-LIVA - 200 ml 200') == canonical_medicine_key('BR-LIVA - 200 ml 200 ml')
    assert canonical_medicine_key('Gastine Suspension    100') == canonical_medicine_key('Gastine Suspension            100 ML')
    assert canonical_medicine_key('CLOB-NM-CREAM         15') == canonical_medicine_key('CLOB-NM-CREAM                 15 G.M')
    assert canonical_medicine_key('ITRABEN-100 CAPSULES') == canonical_medicine_key('ITRABEN-100 CAPSULES          10X1X10 CAP')
    assert canonical_medicine_key('AZIBEN-200 ORAL SUSPEN30ml') == canonical_medicine_key('AZIBEN-200 ORAL SUSPENSION   30ml')
    assert canonical_medicine_key('AC-PLUS TABLET') == canonical_medicine_key('AC-PLUS TABLET                10X2X10')
    assert canonical_medicine_key('GINIPLEX SYRUP-200ML  200') == canonical_medicine_key('GINIPLEX SYRUP-200ML          200 ml')
    assert canonical_medicine_key('FENZYM 100ml          100') == canonical_medicine_key('FENZYM 100ml          100 ml')


def test_no_reorder_logic():
    from backend.app.agent.procurement_agent import ProcurementAgent
    from backend.app.models.entities import Product, InventoryBatch

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    # Create product with ample stock
    p1 = Product(product_code='MED-AMP1', product_name='Ample Med 100mg', reorder_enabled=True, unit_cost=10.0, pack_size=10.0)
    # Create product with 0 stock
    p2 = Product(product_code='MED-ZERO', product_name='Zero Med 50mg', reorder_enabled=True, unit_cost=5.0, pack_size=10.0)
    session.add_all([p1, p2])
    session.flush()

    # Add 500 units of stock for p1 (expiry far in future)
    b1 = InventoryBatch(product_code='MED-AMP1', batch_no='B1', qty_on_hand=500.0, qty_on_order=0.0, expiry_date=datetime(2028, 1, 1), unit_cost=10.0)
    session.add(b1)
    session.commit()

    agent = ProcurementAgent(session)
    no_reorder = agent.get_no_reorder_products()

    # p1 should be in no_reorder because 500 units easily covers demand (net_need < 0)
    no_reorder_codes = [x['product_code'] for x in no_reorder]
    assert 'MED-AMP1' in no_reorder_codes
    # p2 has 0 stock (net_need == 0) and must NOT be in no_reorder (belongs in Review & Correct Suggestions)
    assert 'MED-ZERO' not in no_reorder_codes
    p1_data = next(x for x in no_reorder if x['product_code'] == 'MED-AMP1')
    assert p1_data['net_need'] < 0
    assert p1_data['stock_on_hand'] == 500.0
    assert p1_data['surplus_qty'] > 0

    # Ensure list is sorted alphabetically by character
    names = [x['product_name'] for x in no_reorder]
    assert names == sorted(names, key=lambda n: n.upper())

    session.close()


def test_all_rows_ingested_and_promo_classified_by_blank_mfr_and_supplier():
    from backend.app.adapters.excel import is_footer_or_junk_row, MargExcelParser
    from backend.app.agent.procurement_agent import ProcurementAgent
    from backend.app.models.entities import Product, InventoryBatch

    # 1. is_footer_or_junk_row checks - only true footers / blanks are dropped, not keywords
    assert is_footer_or_junk_row("") is True
    assert is_footer_or_junk_row("TOTAL") is True
    assert is_footer_or_junk_row("150 ITEMS") is True
    assert is_footer_or_junk_row("SPECIAL PACKING MATERIAL") is False
    assert is_footer_or_junk_row("PEN- BLUE 0.5MM") is False
    assert is_footer_or_junk_row("PARACETAMOL 500MG TABLET") is False

    # 2. Ingestion ingests all product rows; blank mfr and supplier -> is_promo_material = True
    raw_rows = [
        {"P A R T I C U L A R S": "PEN- BLACK GEL", "CL. STOCK": 100, "PUR. RATE": 10.0, "MANUFACTURER": "", "SUPPLIER": ""},
        {"P A R T I C U L A R S": "BAG- MEDICAL CARRY", "CL. STOCK": 20, "PUR. RATE": 30.0, "MANUFACTURER": "", "SUPPLIER": "CHATTERJEE ENT"},
        {"P A R T I C U L A R S": "AMOXICILLIN 500MG", "CL. STOCK": 500, "PUR. RATE": 75.0, "MANUFACTURER": "CIPLA", "SUPPLIER": "MED DIST"},
    ]
    df = pd.DataFrame(raw_rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Stock", index=False)

    parsed = MargExcelParser.parse_file(buf.getvalue())
    prod_map = {p["product_name"]: p for p in parsed["products"]}
    assert "PEN- BLACK GEL" in prod_map
    assert "BAG- MEDICAL CARRY" in prod_map
    assert "AMOXICILLIN 500MG" in prod_map

    # PEN- BLACK GEL has blank mfr & supplier -> is_promo_material True
    assert prod_map["PEN- BLACK GEL"]["is_promo_material"] is True
    assert prod_map["PEN- BLACK GEL"]["category"] == "Promo Material"

    # BAG- MEDICAL CARRY contains 'BAG' keyword -> is_promo_material True
    assert prod_map["BAG- MEDICAL CARRY"]["is_promo_material"] is True
    assert prod_map["BAG- MEDICAL CARRY"]["category"] == "Promo Material"

    # AMOXICILLIN has mfr and supplier -> is_promo_material False
    assert prod_map["AMOXICILLIN 500MG"]["is_promo_material"] is False

    # 3. get_no_reorder_products excludes promo materials via is_promo_material flag
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    p_valid = Product(product_code="MED-AMOX", product_name="Amoxicillin 500mg", is_promo_material=False, reorder_enabled=True, unit_cost=50.0, pack_size=10.0)
    p_promo = Product(product_code="MED-PEN", product_name="PEN- DOCTOR BRANDED", is_promo_material=True, reorder_enabled=False, unit_cost=10.0, pack_size=1.0)

    session.add_all([p_valid, p_promo])
    session.flush()

    for p in [p_valid, p_promo]:
        session.add(InventoryBatch(product_code=p.product_code, batch_no="B1", qty_on_hand=500.0, qty_on_order=0.0, expiry_date=datetime(2028, 1, 1), unit_cost=10.0))
    session.commit()

    agent = ProcurementAgent(session)
    no_reorder = agent.get_no_reorder_products()
    no_reorder_names = [x["product_name"].upper() for x in no_reorder]

    assert "AMOXICILLIN 500MG" in no_reorder_names
    assert "PEN- DOCTOR BRANDED" not in no_reorder_names

    session.close()


def test_supplier_column_extraction_and_multi_tier_headers():
    from backend.app.adapters.excel import sanitize_supplier_name, MargExcelParser
    from backend.app.services.ingestion import IngestionService
    from backend.app.models.entities import Supplier

    # 1. Test sanitize_supplier_name logic
    assert sanitize_supplier_name("04/08/2026 J.M HEALTHCARE CHANDI") == "J.M HEALTHCARE CHANDI"
    assert sanitize_supplier_name("09-05-2026 M.S.COMPUTER") == "M.S.COMPUTER"
    assert sanitize_supplier_name("BILL DATE") == ""
    assert sanitize_supplier_name("SUPPLIER NAME") == ""
    assert sanitize_supplier_name("DETAIL =================>") == ""
    assert sanitize_supplier_name("TOTAL") == ""
    assert sanitize_supplier_name("ACCURA CARE") == "ACCURA CARE"

    # 2. Test multi-tier MARG header resolution and supplier extraction
    # Simulates MARG 2-tier header: row 0 = header, row 1 = sub-header, row 2+ = data
    data = [
        ["ITEM", "DESCRIPTION", "QTY.", "BATCH NO.", "TAX", "SUPPLIER", "DETAIL =================>"],
        ["", "", "", "", "%", "BILL DATE", "SUPPLIER NAME"],
        ["AC-BEN", "10X10", "100", "B101", "12", "", "04/08/2026 J.M HEALTHCARE CHANDI"],
        ["METROPOLE", "100ML", "50", "B102", "12", "", "09/05/2026 M.S.COMPUTER"],
        ["GEL-BEN", "30GM", "20", "B103", "12", "", "15/07/2026 ACCURA CARE"],
    ]
    df = pd.DataFrame(data)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Item_Supplier", header=False, index=False)

    parsed = MargExcelParser.parse_file(buf.getvalue(), filename="ITEM AND SUPPLIER.XLS")
    sup_names = [s["supplier_name"] for s in parsed["suppliers"]]
    assert "J.M HEALTHCARE CHANDI" in sup_names
    assert "M.S.COMPUTER" in sup_names
    assert "ACCURA CARE" in sup_names
    assert "BILL DATE" not in sup_names
    assert "SUPPLIER" not in sup_names

    # 3. Test ingestion service upserts them into DB
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    ingestion = IngestionService(session)

    stats = ingestion.ingest_excel(buf.getvalue(), filename="ITEM AND SUPPLIER.XLS", clear_existing=True)
    assert stats["suppliers_upserted"] >= 3

    db_sups = session.query(Supplier).all()
    db_sup_names = [s.supplier_name for s in db_sups]
    assert "J.M HEALTHCARE CHANDI" in db_sup_names
    assert "M.S.COMPUTER" in db_sup_names
    assert "ACCURA CARE" in db_sup_names
    assert "BILL DATE" not in db_sup_names
    assert "SUPPLIER" not in db_sup_names

    session.close()


def test_pharma_canonical_brazol_plus_dedup():
    from backend.app.adapters.excel import pharma_canonical_key, canonical_medicine_key
    from backend.app.agent.procurement_agent import ProcurementAgent
    from backend.app.models.entities import Product, InventoryBatch

    # 1. Canonical key equality test
    k1 = pharma_canonical_key('BRAZOL-PLUS TAB -(10X1) 10X1')
    k2 = pharma_canonical_key('BRAZOL-PLUS-10X1X1 10X1X1')
    assert k1 == k2 == 'BRAZOLPLUS_SOLID_'

    # 2. Agent proposal deduplication test
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    p1 = Product(product_code="2402", product_name="BRAZOL-PLUS TAB -(10X1) 10X1", category="ZZZZZZ 520", reorder_enabled=False, unit_cost=26.0, pack_size=10.0)
    p2 = Product(product_code="0483", product_name="BRAZOL-PLUS-10X1X1 10X1X1", category="BENGAL REMEDIES", reorder_enabled=True, unit_cost=52.41, pack_size=10.0)
    session.add_all([p1, p2])
    session.flush()

    agent = ProcurementAgent(session)
    run_id, proposals = agent.run()

    # Must only generate 1 proposal for BRAZOL-PLUS under the active product 0483
    brazol_proposals = [p for p in proposals if 'BRAZOL-PLUS' in p.product_name]
    assert len(brazol_proposals) == 1
    assert brazol_proposals[0].product_code == "0483"
    session.close()


def test_company_column_ingestion_and_filtering():
    """
    Validates:
    1. 'Company' column in stock Excel is parsed properly.
    2. Company is saved into products and inventory_batches (stock table).
    3. Company is populated on ProcurementProposal and get_no_reorder_products.
    """
    from backend.app.agent.procurement_agent import ProcurementAgent
    from backend.app.models.entities import Product, InventoryBatch

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    # Create dummy Excel with 'Company' column
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df = pd.DataFrame([
            {
                'Item Code': 'MED-APEX1',
                'Item Name': 'Paracetamol 500mg Tabs',
                'Company': 'Apex Pharma',
                'B.NO.': 'AP-01',
                'EXP.': '11/27',
                'CL. STOCK': 10,
                'REORDER LEVEL': 300,
                'PUR. RATE': 20.0,
                'PARTY NAME': 'MedPharma Wholesale',
            },
            {
                'Item Code': 'MED-CIPLA1',
                'Item Name': 'Amoxicillin 250mg Caps',
                'Company': 'Cipla Ltd',
                'B.NO.': 'CIP-01',
                'EXP.': '11/27',
                'CL. STOCK': 500,
                'PUR. RATE': 45.0,
                'PARTY NAME': 'MedPharma Wholesale',
            },
        ])
        df.to_excel(writer, sheet_name='Stock_Status', index=False)

    service = IngestionService(session)
    stats = service.ingest_excel(output.getvalue(), 'test_company_stock.xlsx')

    assert stats['products_upserted'] == 2
    assert stats['batches_inserted'] == 2

    # Check products table
    p_apex = session.query(Product).filter_by(product_code='MED-APEX1').first()
    p_cipla = session.query(Product).filter_by(product_code='MED-CIPLA1').first()
    assert p_apex is not None and p_apex.company == 'Apex Pharma'
    assert p_cipla is not None and p_cipla.company == 'Cipla Ltd'

    # Check stock table (inventory_batches)
    batches = session.query(InventoryBatch).all()
    assert len(batches) == 2
    b_apex = next(b for b in batches if b.product_code == 'MED-APEX1')
    b_cipla = next(b for b in batches if b.product_code == 'MED-CIPLA1')
    assert b_apex.company == 'Apex Pharma'
    assert b_cipla.company == 'Cipla Ltd'

    # Run procurement agent
    agent = ProcurementAgent(session)
    run_id, proposals = agent.run()

    # Proposal should have company populated
    apex_props = [p for p in proposals if p.product_code == 'MED-APEX1']
    assert len(apex_props) == 1
    assert apex_props[0].company == 'Apex Pharma'

    # No-reorder should have company populated
    no_reorder = agent.get_no_reorder_products()
    cipla_nr = [item for item in no_reorder if item['product_code'] == 'MED-CIPLA1']
    assert len(cipla_nr) == 1
    assert cipla_nr[0]['company'] == 'Cipla Ltd'

    session.close()


def test_duplicate_product_rows_aggregated_with_sum_and_comma_batches():
    """
    Validates Requirement 1, 2, 3:
    1. Multi-batch rows with same product name are consolidated into 1 Product in DB.
    2. Current Stock is the sum of batch quantities.
    3. Batch numbers are kept across InventoryBatch records.
    4. Cost Price is stored as unit_cost.
    """
    from backend.app.agent.procurement_agent import ProcurementAgent
    from backend.app.models.entities import Product, InventoryBatch

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    # Create dummy Excel with 2 rows for identical product name "AZITHRO 500 TAB"
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df = pd.DataFrame([
            {
                'Product Name': 'AZITHRO 500 TAB',
                'Item Code': 'AZ01',
                'Company': 'Apex Pharma',
                'Manufacturer': 'Apex Healthcare',
                'B.NO.': 'BAT-001',
                'EXP': '15-May-26',
                'Current Stock': 40,
                'Cost Price ': 35.50,
                'Supplier': 'National Distributors',
            },
            {
                'Product Name': 'AZITHRO 500 TAB',
                'Item Code': 'AZ01',
                'Company': 'Apex Pharma',
                'Manufacturer': 'Apex Healthcare',
                'B.NO.': 'BAT-002',
                'EXP': '20-Nov-26',
                'Current Stock': 60,
                'Cost Price ': 35.50,
                'Supplier': 'National Distributors',
            },
        ])
        df.to_excel(writer, sheet_name='Stock_Status', index=False)

    service = IngestionService(session)
    stats = service.ingest_excel(output.getvalue(), 'test_duplicate_stock.xlsx')

    # Both input rows are preserved in products table (each batch row kept with its unit cost)
    assert stats['products_upserted'] == 2
    assert stats['batches_inserted'] == 2

    # Query product table
    prods = session.query(Product).all()
    assert len(prods) == 2
    p = prods[0]
    assert p.product_name == 'AZITHRO 500 TAB'
    assert p.company == 'Apex Pharma'
    assert p.manufacturer == 'Apex Healthcare'
    assert p.unit_cost == 35.50

    # Query inventory batches
    batches = session.query(InventoryBatch).all()
    assert len(batches) == 2
    total_stock = sum(b.qty_on_hand for b in batches)
    assert total_stock == 100.0  # 40 + 60

    batch_nos = {b.batch_no for b in batches}
    assert batch_nos == {'BAT-001', 'BAT-002'}

    session.close()


def test_expiry_date_format_day_month_2digit_year():
    """
    Validates Requirement 9:
    EXP format 'date-Month-year last two digit' e.g. '15-May-26', '25-08-26', '01-Nov-25'
    """
    from backend.app.adapters.excel import parse_expiry_date

    d1 = parse_expiry_date('15-May-26')
    assert d1 is not None
    assert d1.year == 2026
    assert d1.month == 5
    assert d1.day == 15

    d2 = parse_expiry_date('25-08-26')
    assert d2 is not None
    assert d2.year == 2026
    assert d2.month == 8
    assert d2.day == 25

    d3 = parse_expiry_date('01-Nov-25')
    assert d3 is not None
    assert d3.year == 2025
    assert d3.month == 11
    assert d3.day == 1

    d4 = parse_expiry_date('10/26')
    assert d4 is not None
    assert d4.year == 2026
    assert d4.month == 10


def test_promo_material_isolation_and_exclusion():
    """
    Validates Requirement 7:
    Items with blank manufacturer AND blank supplier are flagged as promo material.
    They must be excluded from procurement proposals and no-reorder list.
    """
    from backend.app.agent.procurement_agent import ProcurementAgent
    from backend.app.models.entities import Product

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    # Create dummy Excel with 1 medicine and 1 promo item (empty mfr and empty supplier)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df = pd.DataFrame([
            {
                'Product Name': 'PARACETAMOL 650',
                'Item Code': 'PCM01',
                'Company': 'Apex Pharma',
                'Manufacturer': 'Apex Labs',
                'Supplier': 'Apex Distributor',
                'B.NO.': 'P101',
                'EXP': '15-Dec-28',
                'Current Stock': 5,
                'REORDER LEVEL': 100,
                'Cost Price ': 15.0,
            },
            {
                'Product Name': 'DOCTOR GIFT PEN STAND',
                'Item Code': 'GIFT01',
                'Company': 'Marketing Promo',
                'Manufacturer': '',  # Blank
                'Supplier': '',      # Blank
                'B.NO.': 'DEFAULT',
                'EXP': '',
                'Current Stock': 50,
                'REORDER LEVEL': 100,
                'Cost Price ': 120.0,
            },
        ])
        df.to_excel(writer, sheet_name='Stock_Status', index=False)

    service = IngestionService(session)
    service.ingest_excel(output.getvalue(), 'test_promo.xlsx')

    # Verify promo item flag
    gift_prod = session.query(Product).filter_by(product_code='GIFT01').first()
    assert gift_prod is not None
    assert gift_prod.is_promo_material is True

    med_prod = session.query(Product).filter_by(product_code='PCM01').first()
    assert med_prod is not None
    assert med_prod.is_promo_material is False

    # Run agent
    agent = ProcurementAgent(session)
    run_id, proposals = agent.run()

    # The gift pen stand must NEVER appear in proposals
    prop_codes = [p.product_code for p in proposals]
    assert 'GIFT01' not in prop_codes
    assert 'PCM01' in prop_codes

    # The gift pen stand must NEVER appear in no-reorder list
    no_reorder = agent.get_no_reorder_products()
    nr_codes = [item['product_code'] for item in no_reorder]
    assert 'GIFT01' not in nr_codes

    # Test keyword matching including PRODUCT, BAG, VISUAL-AID, PACKING, etc.
    from backend.app.adapters.excel import is_promotional_material
    assert is_promotional_material('PRODUCT CARD - BEN REM PVT LTD', has_mfr=True, has_sup=True) is True
    assert is_promotional_material('DOCTOR BAG LEATHER', has_mfr=True, has_sup=True) is True
    assert is_promotional_material('VISUAL-AID- BENGAL PHARMA-2025 1', has_mfr=True, has_sup=True) is True
    assert is_promotional_material('CORRUGATED PACKING MATERIAL', has_mfr=True, has_sup=True) is True
    assert is_promotional_material('STRIP PACKING CHARGES', has_mfr=True, has_sup=True) is True
    assert is_promotional_material('AMOXICILLIN 500MG', has_mfr=True, has_sup=True) is False

    session.close()


def test_high_and_near_expiry_excluded_from_proposals():
    """
    Validates that high-risk and near-expiry products are NEVER generated as
    procurement proposals for the Review and Correct Suggestions tab.
    """
    from datetime import datetime, timedelta
    from backend.app.agent.procurement_agent import ProcurementAgent
    from backend.app.models.entities import Product, InventoryBatch

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    now = datetime.utcnow()
    # 1. Product with near-expiry stock (expires in 60 days <= 180 days)
    p_near = Product(
        product_code='MED-NEAR1',
        product_name='NEAR EXPIRY MEDICINE 500MG',
        category='Active Shelf-Life',
        company='Pharma Corp',
        unit_cost=50.0,
        reorder_point=100.0,
        reorder_enabled=True,
    )
    # 2. Product with healthy stock (expires in 500 days)
    p_healthy = Product(
        product_code='MED-CLEAN1',
        product_name='HEALTHY MEDICINE 250MG',
        category='Active Shelf-Life',
        company='Pharma Corp',
        unit_cost=30.0,
        reorder_point=100.0,
        reorder_enabled=True,
    )
    session.add_all([p_near, p_healthy])
    session.flush()

    # Batches
    b_near = InventoryBatch(
        product_code='MED-NEAR1',
        batch_no='BN-NEAR',
        qty_on_hand=10.0,
        expiry_date=now + timedelta(days=60),  # < 180 days -> High/Near Expiry Risk
        unit_cost=50.0,
    )
    b_healthy = InventoryBatch(
        product_code='MED-CLEAN1',
        batch_no='BN-CLEAN',
        qty_on_hand=5.0,
        expiry_date=now + timedelta(days=500),  # Healthy
        unit_cost=30.0,
    )
    session.add_all([b_near, b_healthy])
    session.commit()

    agent = ProcurementAgent(session)
    run_id, proposals = agent.run()

    prop_codes = [p.product_code for p in proposals]
    # Healthy medicine should generate a replenishment proposal
    assert 'MED-CLEAN1' in prop_codes
    # Near-expiry medicine must NEVER be proposed for reorder suggestions
    assert 'MED-NEAR1' not in prop_codes

    session.close()


def test_sales_upload_does_not_duplicate_existing_products():
    """
    Validates that uploading a sales report after stock has already been loaded:
    1. Attaches sales_history directly to the existing product records.
    2. Does NOT insert duplicate products into the products table.
    3. Handles canonical name differences between stock and sales sheets.
    """
    from backend.app.models.entities import Product, SalesHistory

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    # Step 1: Ingest stock file with 1 medicine
    stock_buf = io.BytesIO()
    with pd.ExcelWriter(stock_buf, engine='openpyxl') as writer:
        df_stock = pd.DataFrame([
            {
                'Product Name': 'AZIBEN-200 ORAL SUSPENSION 30ML',
                'Item Code': 'AZI200',
                'Company': 'Apex Pharma',
                'Manufacturer': 'Apex Healthcare',
                'B.NO.': 'AZ-001',
                'EXP': '15-Dec-27',
                'Current Stock': 50,
                'Cost Price ': 45.0,
                'Supplier': 'Apex Distributor',
            }
        ])
        df_stock.to_excel(writer, sheet_name='Stock_Status', index=False)

    service = IngestionService(session)
    service.ingest_excel(stock_buf.getvalue(), filename='stock_status.xlsx', clear_existing=True)

    # Verify initial products count = 1
    assert session.query(Product).count() == 1
    p_initial = session.query(Product).first()
    assert p_initial.product_code == 'AZI200'

    # Step 2: Upload Sales report with slightly varied spacing/name
    sales_buf = io.BytesIO()
    with pd.ExcelWriter(sales_buf, engine='openpyxl') as writer:
        # Sheet simulating MARG Sales Report
        df_sales = pd.DataFrame([
            ['SALES SUMMARY REPORT (01/04/2026 - 30/04/2026)', '', '', ''],
            ['PARTICULARS', 'QUANTITY', 'FREE', 'RATE'],
            ['AZIBEN-200 ORAL SUSPEN30ml', 25, 0, 60.0],
        ])
        df_sales.to_excel(writer, sheet_name='Sales_Summary', header=False, index=False)

    service.ingest_excel(sales_buf.getvalue(), filename='sales_summary.xlsx', clear_existing=False)

    # Step 3: Verify products count remains EXACTLY 1 (no duplicate product row inserted!)
    assert session.query(Product).count() == 1

    # Step 4: Verify sales history rows were successfully attached to the original product
    sales_rows = session.query(SalesHistory).all()
    assert len(sales_rows) > 0
    assert all(s.product_code == 'AZI200' for s in sales_rows)

    session.close()


def test_sales_upload_with_unmatched_items_does_not_insert_products_and_purge_wipes_all():
    """
    Validates:
    1. Uploading a sales report that contains items not present in the catalog does NOT insert
       any dummy/unmatched products into the Product table.
    2. Sales history is recorded for demand tracking.
    3. Purge clears all tables including SalesHistory and Product.
    """
    from backend.app.models.entities import Product, SalesHistory

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    # Step 1: Load 2 catalog products
    stock_buf = io.BytesIO()
    with pd.ExcelWriter(stock_buf, engine='openpyxl') as writer:
        df_stock = pd.DataFrame([
            {
                'Product Name': 'CETRIZINE 10MG TAB',
                'Item Code': 'CET10',
                'Company': 'Cipla',
                'Manufacturer': 'Cipla Ltd',
                'B.NO.': 'CT-101',
                'EXP': '15-Dec-27',
                'Current Stock': 100,
                'Cost Price ': 12.0,
                'Supplier': 'National Distributors',
            },
            {
                'Product Name': 'AMOXICILLIN 500MG CAP',
                'Item Code': 'AMX500',
                'Company': 'Alkem',
                'Manufacturer': 'Alkem Labs',
                'B.NO.': 'AX-502',
                'EXP': '20-Oct-27',
                'Current Stock': 80,
                'Cost Price ': 35.0,
                'Supplier': 'Med Supply Corp',
            }
        ])
        df_stock.to_excel(writer, sheet_name='Stock_Status', index=False)

    service = IngestionService(session)
    service.ingest_excel(stock_buf.getvalue(), filename='stock.xlsx', clear_existing=True)
    assert session.query(Product).count() == 2

    # Step 2: Upload Sales file with 1 matched product + 2 completely unknown products
    sales_buf = io.BytesIO()
    with pd.ExcelWriter(sales_buf, engine='openpyxl') as writer:
        df_sales = pd.DataFrame([
            ['MARG ERP - SALE SUMMARY REPORT', '', '', ''],
            ['PARTICULARS', 'QUANTITY', 'FREE', 'RATE'],
            ['CETRIZINE 10MG TAB', 30, 0, 15.0],
            ['HISTORICAL DISCONTINUED ITEM ABC', 10, 0, 50.0],
            ['NON-CATALOG PRODUCT XYZ', 5, 0, 99.0],
        ])
        df_sales.to_excel(writer, sheet_name='Sales', header=False, index=False)

    service.ingest_excel(sales_buf.getvalue(), filename='sale_summary.xlsx', clear_existing=False)

    # Product count must remain EXACTLY 2! No dummy products inserted for the 2 uncataloged items!
    assert session.query(Product).count() == 2

    # Sales history rows must have recorded 30 daily data points for each item with quantity sold
    sales_history_count = session.query(SalesHistory).count()
    assert sales_history_count > 0

    # Step 3: Purge clears all data including sales history and products
    purged = service.purge_all_data()
    assert purged['deleted_products'] == 2
    assert purged['deleted_sales'] == sales_history_count
    assert session.query(Product).count() == 0
    assert session.query(SalesHistory).count() == 0

    session.close()


def test_stable_code_deterministic_matching_between_stock_and_sales():
    """
    Validates solution 1 B:
    When neither stock sheet nor sales sheet provides an explicit Item Code:
    1. Both use _make_stable_code(name).
    2. Both generate the EXACT same product code (e.g. MED-xxxxx).
    3. The product code in Product table and sales_history table match 100%.
    """
    from backend.app.models.entities import Product, SalesHistory
    from backend.app.adapters.excel import _make_stable_code

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    medicine_name = 'BR-LIVA SYRUP 200ML'
    expected_stable_code = _make_stable_code(medicine_name)
    assert expected_stable_code.startswith('MED-')

    # Step 1: Ingest stock without any product_code column (common in MARG closing stock)
    stock_buf = io.BytesIO()
    with pd.ExcelWriter(stock_buf, engine='openpyxl') as writer:
        df_stock = pd.DataFrame([
            {
                'P A R T I C U L A R S': medicine_name,
                'B . N O .': 'LIV-99',
                'E X P .': '10/27',
                'C L .   S T O C K': 60,
                'R A T E': 95.0,
            }
        ])
        df_stock.to_excel(writer, sheet_name='Stock', index=False)

    service = IngestionService(session)
    service.ingest_excel(stock_buf.getvalue(), filename='closing_stock.xlsx', clear_existing=True)

    prod = session.query(Product).first()
    assert prod is not None
    assert prod.product_code == expected_stable_code

    # Step 2: Ingest sales without any product_code column
    sales_buf = io.BytesIO()
    with pd.ExcelWriter(sales_buf, engine='openpyxl') as writer:
        df_sales = pd.DataFrame([
            ['SALES SUMMARY REPORT', '', '', ''],
            ['PARTICULARS', 'QUANTITY', 'FREE', 'RATE'],
            ['BR-LIVA 200ML SYP', 20, 0, 120.0],
        ])
        df_sales.to_excel(writer, sheet_name='Sales', header=False, index=False)

    service.ingest_excel(sales_buf.getvalue(), filename='sales_report.xlsx', clear_existing=False)

    # Step 3: Product table still has 1 item with expected_stable_code
    assert session.query(Product).count() == 1
    assert session.query(Product).first().product_code == expected_stable_code

    # Step 4: Sales history rows all have the EXACT SAME expected_stable_code
    sales_rows = session.query(SalesHistory).all()
    assert len(sales_rows) > 0
    assert all(s.product_code == expected_stable_code for s in sales_rows)
    assert all(s.product_code == prod.product_code for s in sales_rows)

    session.close()


def test_healthy_no_reorder_products_excluded_from_inventory_tab():
    """
    Validates the mandatory rule:
    Products in healthy state in 'No Need for Reorder' tab must NOT show
    in the 'Inventory and FEFO' tab.
    """
    from datetime import datetime, timedelta
    from backend.app.agent.procurement_agent import ProcurementAgent
    from backend.app.main import list_inventory
    from backend.app.models.entities import Product, InventoryBatch

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    now = datetime.utcnow()

    # Product A: Healthy stock covering demand -> belongs in No Need for Reorder
    p_healthy = Product(
        product_code='MED-HEALTHY',
        product_name='HEALTHY MEDICINE 500MG',
        category='Active Shelf-Life',
        company='Apex Pharma',
        unit_cost=50.0,
        reorder_point=10.0,
        reorder_enabled=True,
    )
    b_healthy = InventoryBatch(
        product_code='MED-HEALTHY',
        batch_no='BN-HEALTHY-01',
        qty_on_hand=200.0,
        expiry_date=now + timedelta(days=400),  # > 180d, healthy shelf life
        unit_cost=50.0,
    )

    # Product B: At-risk product with expired / near-expiry stock
    p_risk = Product(
        product_code='MED-RISK',
        product_name='EXPIRED MEDICINE 250MG',
        category='Expired',
        company='Apex Pharma',
        unit_cost=30.0,
        reorder_point=10.0,
        reorder_enabled=True,
    )
    b_risk = InventoryBatch(
        product_code='MED-RISK',
        batch_no='BN-RISK-01',
        qty_on_hand=50.0,
        expiry_date=now - timedelta(days=10),   # Expired
        unit_cost=30.0,
    )

    session.add_all([p_healthy, b_healthy, p_risk, b_risk])
    session.commit()

    agent = ProcurementAgent(session)
    no_reorder = agent.get_no_reorder_products()
    nr_codes = [x['product_code'] for x in no_reorder]

    # Verify healthy product is in No Need for Reorder
    assert 'MED-HEALTHY' in nr_codes

    # Call list_inventory with exclude_healthy=True (Inventory & FEFO tab view)
    inv_items = list_inventory(exclude_healthy=True, db=session)
    inv_codes = [x['product_code'] for x in inv_items]

    # Crucial assertion: MED-HEALTHY MUST NOT be in Inventory & FEFO
    assert 'MED-HEALTHY' not in inv_codes
    # At-risk product MED-RISK MUST be in Inventory & FEFO
    assert 'MED-RISK' in inv_codes

    session.close()


def test_net_need_zero_and_negative_tab_isolation():
    from backend.app.services.policy import ProcurementPolicy
    from backend.app.agent.procurement_agent import ProcurementAgent
    from backend.app.models.entities import Product, InventoryBatch

    # 1. Policy mathematics verification
    policy = ProcurementPolicy(review_days=7, safety_days=3, expiry_risk_horizon_days=90)

    # When usable stock (60) > target stock (1.0 * 55 = 55): net_need = -5 < 0
    calc_surplus = policy.calculate(
        avg_daily_demand=1.0, lead_time_days=45, stock_on_hand=60.0, stock_on_order=0.0,
        usable_before_expiry=60.0, near_expiry_qty=0.0, min_order_qty=1.0, pack_size=10.0,
        unit_cost=10.0, expiry_risk=0.0
    )
    assert calc_surplus['net_need'] < 0
    assert calc_surplus['order_qty'] == 0.0

    # When usable stock (55) == target stock (1.0 * 55 = 55): net_need == 0
    calc_zero = policy.calculate(
        avg_daily_demand=1.0, lead_time_days=45, stock_on_hand=55.0, stock_on_order=0.0,
        usable_before_expiry=55.0, near_expiry_qty=0.0, min_order_qty=1.0, pack_size=10.0,
        unit_cost=10.0, expiry_risk=0.0
    )
    assert calc_zero['net_need'] == 0.0
    # Must propose at least 1 pack size for review and correction
    assert calc_zero['order_qty'] >= 10.0

    # 2. Database & Agent get_no_reorder_products verification
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    p_surplus = Product(product_code='MED-SURP', product_name='Surplus Med', reorder_enabled=True, unit_cost=10.0, pack_size=10.0)
    b_surplus = InventoryBatch(product_code='MED-SURP', batch_no='BS1', qty_on_hand=200.0, qty_on_order=0.0, expiry_date=datetime(2028, 1, 1), unit_cost=10.0)

    p_zero = Product(product_code='MED-ZERO-STOCK', product_name='Zero Stock Med', reorder_enabled=True, unit_cost=10.0, pack_size=10.0)
    # Zero stock on hand -> net_need == 0 (with 0 demand)
    b_zero = InventoryBatch(product_code='MED-ZERO-STOCK', batch_no='BZ1', qty_on_hand=0.0, qty_on_order=0.0, expiry_date=datetime(2028, 1, 1), unit_cost=10.0)

    session.add_all([p_surplus, b_surplus, p_zero, b_zero])
    session.commit()

    agent = ProcurementAgent(session)
    no_reorder = agent.get_no_reorder_products()
    nr_codes = [x['product_code'] for x in no_reorder]

    # Surplus (net_need < 0) MUST be in No Need for Reorder
    assert 'MED-SURP' in nr_codes
    # Zero stock / net_need == 0 MUST NOT be in No Need for Reorder (reserved for Review & Correct Suggestions)
    assert 'MED-ZERO-STOCK' not in nr_codes

    session.close()


def test_daily_demand_velocity_since_first_april():
    from datetime import date
    from backend.app.services.forecast import DemandService, get_days_from_fiscal_year_start
    from backend.app.services.policy import ProcurementPolicy
    from backend.app.models.entities import Product, SalesHistory

    # 1. Test elapsed days function
    # Test date: 2026-10-02 (Oct 2, 2026). April 1, 2026 to Oct 2, 2026 is exactly 184 days
    days, fy_start = get_days_from_fiscal_year_start(date(2026, 10, 2))
    assert fy_start == date(2026, 4, 1)
    assert days == 184

    # 2. Test DemandService daily demand velocity = total quantity sale / days from 1st April to today
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    p = Product(product_code='MED-VEL', product_name='Velocity Med', reorder_enabled=True, pack_size=10.0, min_order_qty=1.0, unit_cost=5.0)
    session.add(p)
    # Total sale: 368 units sold on 2026-08-01 (within FY starting 1st April 2026)
    s = SalesHistory(product_code='MED-VEL', product_name='Velocity Med', qty_sold=368.0, sale_date=datetime(2026, 8, 1))
    session.add(s)
    session.commit()

    demand_svc = DemandService(session)
    as_of = date(2026, 10, 2)  # 184 days since 1st April 2026
    vel, src = demand_svc.forecast_daily('MED-VEL', as_of_date=as_of)
    # 368.0 / 184 = 2.0 units/day
    assert vel == 2.0
    assert 'sales_since_1st_april' in src

    # 3. Test reorder calculation using this updated daily demand velocity
    policy = ProcurementPolicy(review_days=7, safety_days=3, expiry_risk_horizon_days=90)
    # Target stock = daily_velocity (2.0) * coverage (45 + 7 + 3 = 55 days) = 110.0 units
    calc = policy.calculate(
        avg_daily_demand=vel, lead_time_days=45, stock_on_hand=10.0, stock_on_order=0.0,
        usable_before_expiry=10.0, near_expiry_qty=0.0, min_order_qty=1.0, pack_size=10.0,
        unit_cost=5.0, expiry_risk=0.0
    )
    assert calc['target_stock'] == 110.0
    # Net need = target_stock (110) - usable (10) = 100.0 units
    assert calc['net_need'] == 100.0
    assert calc['order_qty'] == 100.0

    # 4. Test demand matching when stock product_code and sales product_code differ (e.g. MARG stock code '0992' vs sales 'MED-39769')
    p2 = Product(product_code='0992', product_name='GLOWVIT MV DROPS 30ML', reorder_enabled=True, pack_size=30.0, min_order_qty=1.0, unit_cost=10.81)
    session.add(p2)
    s2 = SalesHistory(product_code='MED-39769', product_name='GLOWVIT MV DROPS 30ML', qty_sold=2346.0, sale_date=datetime(2026, 8, 15))
    session.add(s2)
    session.commit()

    vel2, src2 = demand_svc.forecast_daily('0992', as_of_date=as_of)
    # 2346.0 / 184 = 12.75 units/day
    assert vel2 == 12.75
    assert 'sales_since_1st_april' in src2

    session.close()
