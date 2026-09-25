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
    if not os.path.exists(path):
        pytest.skip("Manufacturer list test file not in Downloads")

    with open(path, 'rb') as f:
        content = f.read()

    parsed = MargExcelParser.parse_file(content)
    assert len(parsed['suppliers']) >= 20
    # First supplier should have parsed clean name and GMP reliability
    sup0 = parsed['suppliers'][0]
    assert 'ACCURA' in sup0['supplier_name']
    assert sup0['lead_time_days'] == 45
    assert sup0['reliability_score'] >= 0.90


def test_ingest_outstanding_if_present():
    path = '/Users/debz/Downloads/outstanding.xlsx'
    if not os.path.exists(path):
        pytest.skip("Outstanding test file not in Downloads")

    with open(path, 'rb') as f:
        content = f.read()

    parsed = MargExcelParser.parse_file(content)
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

    # p1 should be in no_reorder because 500 units easily covers demand
    no_reorder_codes = [x['product_code'] for x in no_reorder]
    assert 'MED-AMP1' in no_reorder_codes
    p1_data = next(x for x in no_reorder if x['product_code'] == 'MED-AMP1')
    assert p1_data['net_need'] <= 0
    assert p1_data['stock_on_hand'] == 500.0
    assert p1_data['surplus_qty'] > 0

    # Ensure list is sorted alphabetically by character
    names = [x['product_name'] for x in no_reorder]
    assert names == sorted(names, key=lambda n: n.upper())

    session.close()


def test_ignore_promotional_and_packing_keywords():
    from backend.app.adapters.excel import is_footer_or_junk_row, MargExcelParser
    from backend.app.agent.procurement_agent import ProcurementAgent
    from backend.app.models.entities import Product, InventoryBatch

    # 1. is_footer_or_junk_row checks
    assert is_footer_or_junk_row("SPECIAL PACKING MATERIAL") is True
    assert is_footer_or_junk_row("PEN- BLUE 0.5MM") is True
    assert is_footer_or_junk_row("COMFORT PILLOW SMALL") is True
    assert is_footer_or_junk_row("BAG- NON WOVEN CARRY") is True
    assert is_footer_or_junk_row("PROMOTIONAL BAG SAMPLE") is True
    assert is_footer_or_junk_row("PARACETAMOL 500MG TABLET") is False

    # 2. Ingestion ignores these rows
    raw_rows = [
        {"P A R T I C U L A R S": "PEN- BLACK GEL", "CL. STOCK": 100, "PUR. RATE": 10.0},
        {"P A R T I C U L A R S": "PILLOW PROMO ITEM", "CL. STOCK": 50, "PUR. RATE": 50.0},
        {"P A R T I C U L A R S": "BAG- MEDICAL CARRY", "CL. STOCK": 20, "PUR. RATE": 30.0},
        {"P A R T I C U L A R S": "CORRUGATED PACKING BOX", "CL. STOCK": 200, "PUR. RATE": 5.0},
        {"P A R T I C U L A R S": "AMOXICILLIN 500MG", "CL. STOCK": 500, "PUR. RATE": 75.0},
    ]
    df = pd.DataFrame(raw_rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Stock", index=False)

    parsed = MargExcelParser.parse_file(buf.getvalue())
    prod_names = [p["product_name"] for p in parsed["products"]]
    assert "AMOXICILLIN 500MG" in prod_names
    assert not any("PEN-" in n for n in prod_names)
    assert not any("PILLOW" in n for n in prod_names)
    assert not any("BAG-" in n for n in prod_names)
    assert not any("PACKING" in n for n in prod_names)

    # 3. get_no_reorder_products ignores them even if present in DB
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    p_valid = Product(product_code="MED-AMOX", product_name="Amoxicillin 500mg", reorder_enabled=True, unit_cost=50.0, pack_size=10.0)
    p_pen = Product(product_code="MED-PEN", product_name="PEN- DOCTOR BRANDED", reorder_enabled=True, unit_cost=10.0, pack_size=1.0)
    p_bag = Product(product_code="MED-BAG", product_name="BAG- CARRY POUCH", reorder_enabled=True, unit_cost=20.0, pack_size=1.0)
    p_pack = Product(product_code="MED-PACK", product_name="BOX PACKING 100S", reorder_enabled=True, unit_cost=5.0, pack_size=1.0)
    p_pillow = Product(product_code="MED-PIL", product_name="ORTHO PILLOW", reorder_enabled=True, unit_cost=100.0, pack_size=1.0)

    session.add_all([p_valid, p_pen, p_bag, p_pack, p_pillow])
    session.flush()

    for p in [p_valid, p_pen, p_bag, p_pack, p_pillow]:
        session.add(InventoryBatch(product_code=p.product_code, batch_no="B1", qty_on_hand=500.0, qty_on_order=0.0, expiry_date=datetime(2028, 1, 1), unit_cost=10.0))
    session.commit()

    agent = ProcurementAgent(session)
    no_reorder = agent.get_no_reorder_products()
    no_reorder_names = [x["product_name"].upper() for x in no_reorder]

    assert "AMOXICILLIN 500MG" in no_reorder_names
    for junk_kw in ["PACKING", "PEN-", "PILLOW", "BAG-"]:
        assert not any(junk_kw in n for n in no_reorder_names)

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



