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
