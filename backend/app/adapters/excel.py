"""
MARG Excel Ingestion Adapter

Parses real-world MARG ERP Excel exports (.xlsx, .xls, .csv) for:
- Closing Stock / Rates Statements with Batches & Expiry (e.g. STOCK.XLS)
- Sales Summary reports for demand velocity (e.g. SALES REPORT.XLS)
- Purchase Summary reports for actual procurement costs (e.g. PURCHASE SUMMARY.XLS)
- Supplier & Manufacturer master lists (e.g. MANUFACTURER LIST.xls)
- PCD Outstanding reports (e.g. outstanding.xlsx)

Handles MARG-specific nuances:
- Report title banners in top rows (e.g. 'SALES SUMMARY', 'STOCK & RATES STATEMENT')
- Generic sheet names (e.g. 'MARG ERP AI+ Excel Report', 'Sheet1')
- Column names with spaced-out letters (e.g. 'P A R T I C U L A R S', 'C L .   S T O C K')
- Flexible column naming / fuzzy alias matching with non-alphanumeric collapsing
- Pharmaceutical expiry formats (MM/YY, MM/YYYY, Mon-YY, DD/MM/YYYY, DD-MM-YYYY)
- Multi-pack formats (1*10, 10'S, 10X10, 100 ML)
"""
from __future__ import annotations
import io
import re
import hashlib
import calendar
from datetime import datetime, timedelta
from typing import Any
import pandas as pd
from backend.app.core.config import settings


# Canonical column alias dictionary (collapsed alphanumeric strings)
COLUMN_ALIASES: dict[str, list[str]] = {
    'product_code': [
        'productcode', 'itemcode', 'code', 'pcode', 'itemno', 'procode', 'pron',
        'itemnum', 'product_code'
    ],
    'product_name': [
        'itemdescription', 'particulars', 'itemname', 'productname', 'description',
        'product', 'item', 'itemtitle', 'name', 'product_name'
    ],
    'category': [
        'category', 'group', 'brand', 'type'
    ],
    'company': [
        'company', 'companyname', 'company_name', 'cmpny', 'comp'
    ],
    'manufacturer': [
        'manufacturer', 'manufacturers', 'mfr', 'mfg', 'mfgby', 'manufacturedby',
        'mfrname', 'manufacturername', 'mfg_by'
    ],
    'unit': ['unit', 'uom', 'packingunit'],
    'pack_size': ['packing', 'pack', 'packsize', 'pkg', 'pk', 'packaging', 'pack_size'],
    'min_order_qty': ['moq', 'minorderqty', 'minorder', 'minqty', 'min_order_qty'],
    'reorder_point': [
        'reorderlevel', 'reorderpoint', 'reorderlvl', 'minlevel', 'minstock',
        'orderlevel', 'reorder', 'reorder_point'
    ],
    'unit_cost': [
        'costprice', 'cost_price', 'cost', 'unitcost', 'unit_cost',
        'purchaseprice', 'purrate', 'purchaserate', 'prate',
        'avrate', 'net', 'netrate', 'rate', 'mrp'
    ],
    'batch_no': [
        'batchno', 'bno', 'batch', 'batchnumber', 'lot', 'lotno', 'lotnum', 'batch_no'
    ],
    'expiry_date': [
        'exp', 'expdate', 'expirydate', 'expiry', 'expdt', 'exp_date', 'expiry_date',
        'validity', 'validupto', 'valdate', 'bbd', 'bestbefore'
    ],
    'qty_on_hand': [
        'currentstock', 'currstock', 'current_stock', 'clstock', 'closingstock', 'stock',
        'balance', 'balqty', 'qty', 'onhand', 'qtyonhand', 'qty_on_hand'
    ],
    'qty_on_order': ['qtyonorder', 'onorder', 'pendingpo', 'poqty', 'qty_on_order'],
    'supplier_id': ['supplierid', 'suppliercode', 'partycode', 'vendorid', 'supcode', 'supplier_id'],
    'supplier_name': [
        'suppliername', 'suppliernames', 'nameofsupplier', 'nameofsuppliers',
        'suppliers', 'supplier', 'partyname', 'party', 'vendorname', 'vendor',
        'vendors', 'supplier_name'
    ],
    'lead_time_days': ['leadtimedays', 'leadtime', 'crdays', 'creditdays', 'lead_time_days'],
    'min_order_value': ['minordervalue', 'mov', 'minorder', 'min_order_value'],
    'sale_date': ['saledate', 'date', 'billdate', 'invoicedate', 'dt', 'sale_date'],
    'qty_sold': [
        'quantity', 'qtysold', 'soldqty', 'sold', 'saleqty', 'billedqty',
        'totalbillvaleuptodate', 'totalbill', 'qty_sold'
    ],
    'free_qty': ['free', 'freeqty', 'schemeqty'],
    'channel': ['mrname', 'channel', 'salesman', 'rep'],
    'remarks': ['remarks', 'remark', 'certification', 'notes'],
    'place': ['place', 'location', 'city', 'station'],
    'balance_outstanding': ['balanceoutstanding', 'baloutstanding', 'dueoutstanding'],
    'op_due': ['opdue', 'openingdue'],
}


def _clean_alpha(s: Any) -> str:
    """Strips all non-alphanumeric characters and converts to lowercase for resilient matching."""
    if s is None or pd.isna(s):
        return ''
    return re.sub(r'[^a-z0-9]', '', str(s).lower()).strip()


def _clean_str(val: Any) -> str:
    if val is None or pd.isna(val):
        return ''
    # Normalize internal multiple spaces into single space
    return ' '.join(str(val).split())


MONTH_NAME_MAP: dict[str, int] = {
    'JAN': 1, 'JANUARY': 1,
    'FEB': 2, 'FEBRUARY': 2,
    'MAR': 3, 'MARCH': 3,
    'APR': 4, 'APRIL': 4,
    'MAY': 5,
    'JUN': 6, 'JUNE': 6,
    'JUL': 7, 'JULY': 7,
    'AUG': 8, 'AUGUST': 8,
    'SEP': 9, 'SEPT': 9, 'SEPTEMBER': 9,
    'OCT': 10, 'OCTOBER': 10,
    'NOV': 11, 'NOVEMBER': 11,
    'DEC': 12, 'DECEMBER': 12
}


def canonical_medicine_key(name: str) -> str:
    """
    Extracts the canonical medicine key by normalizing spacing, punctuation,
    dosage forms, volume/weight units, packaging materials, and packing multiples across MARG reports.
    E.g.:
    - 'BR-LIVA - 200 ml      200' and 'BR-LIVA - 200 ml              200 ml' both yield 'BRLIVA'.
    - 'AZIBEN-200 ORAL SUSPEN30ml' and 'AZIBEN-200 ORAL SUSPENSION 30ml' both yield 'AZIBEN200'.
    - 'AC-PLUS TABLET 10X2X10' and 'AC-PLUS TABLET' both yield 'ACPLUS'.
    - 'Gastine Suspension    100' and 'Gastine Suspension 100 ML' both yield 'GASTINE'.
    - 'ITRABEN-100 CAPSULES' and 'ITRABEN-100 CAPSULES 10X1X10 CAP' both yield 'ITRABEN100'.
    """
    if not name:
        return ''
    s = str(name).strip()

    # 0. Separate glued units and trailing numbers caused by fixed-width MARG report column cuts
    # e.g. '200 ML200' -> '200 ML 200', '100ML100' -> '100 ML 100'
    s = re.sub(r'(\d+\s*(?:ML|GM|MG|LTR|LT|KG|M))(\d+)$', r'\1 \2', s, flags=re.I)

    # 1. Strip trailing MARG packaging specification after 2 or more spaces
    parts = re.split(r'\s{2,}', s)
    if len(parts) >= 2:
        last = parts[-1].strip()
        if (re.match(r'^\d+\s*[*xX]\s*\d+', last, re.I) or
            re.match(r'^\d+\s*(?:ML|GM|MG|LTR|LT|KG|M|PCS|TAB|CAP|BTL|VIAL|AMP)?$', last, re.I) or
            re.match(r'^\d+\s*(?:ML|GM|MG|LTR|LT|KG|M|PCS|TAB|CAP|BTL|VIAL|AMP)\b', last, re.I) or
            re.match(r'^(?:ML|GM|MG|LTR|LT|KG|PCS|TAB|CAP|BTL|VIAL|AMP)\b', last, re.I) or
            re.match(r'^\d+$', last)):
            s = ' '.join(parts[:-1]).strip()

    # 2. Strip trailing packaging patterns even if single space or already collapsed
    s = re.sub(r'\s+\d+\s*[*xX]\s*\d+(\s*[*xX]\s*\d+)?(\s+[A-Za-z]+)?$', '', s, flags=re.I)
    s = re.sub(r'\s+\d+\s*\*\s*\d+(\s+[A-Za-z]+)?$', '', s, flags=re.I)
    s = re.sub(r'\s+\d+\'S$', '', s, flags=re.I)
    s = re.sub(r'\s+\d+\s*PCS$', '', s, flags=re.I)

    # 3. Trailing duplicate number even if unit was attached to earlier occurrence
    # e.g. 'GINIPLEX SYRUP-200ML 200' -> 'GINIPLEX SYRUP-200ML'
    #      'BENVITA GOLD -500 ML 500' -> 'BENVITA GOLD -500 ML'
    #      'BR-LIVA - 200 ml 200' -> 'BR-LIVA - 200 ml'
    #      'FENZYM 100ml 100' -> 'FENZYM 100ml'
    m = re.search(r'(\d+)\s*(?:ML|GM|MG|LTR|LT|KG|M|PCS|TAB|CAP)?\b.*\s+(\1)$', s, flags=re.I)
    if m:
        s = re.sub(r'\s+' + re.escape(m.group(2)) + r'$', '', s)

    # 4. Number at end preceded by dosage form or unit
    s = re.sub(r'(\b(?:ML|GM|MG|LTR|LT|KG|M|SYP|SYRUP|SUSP|SUSPEN|SUSPENSION|CREAM|OINT|GEL|DROPS?|INJ|TAB|TABLET|CAP|CAPSU|CAPSULE|CAR|BAG|BANNER|CARTON|LABEL|BOX|BOTTLE|CONTAINER))\s+\d+$', r'\1', s, flags=re.I)
    s = re.sub(r'(?:ML|GM|MG|LTR|LT|KG|M)\s+\d+$', '', s, flags=re.I)

    # 5. Trailing ' 1' pack indicator
    s = re.sub(r'(?<=[A-Za-z\/\-])\s+1$', '', s)

    s = s.upper().strip()

    # 2. Remove parenthetical notes/companies even if unclosed: e.g. (DWARKA PHARMA), (DWARKA P
    s = re.sub(r'\([^\)]*(?:\)|$)', ' ', s)

    # 3. Normalize volume & weight units
    s = s.replace('M.L', 'ML').replace('M.G', 'MG').replace('G.M', 'GM')

    # 4. Remove MRP clauses e.g. MRP-150/-, MR-100, MRP 200
    s = re.sub(r'\bMRP?\s*[-:]?\s*\d+.*', ' ', s)

    # 5. Remove packaging patterns anywhere in the string
    s = re.sub(r'\b\d+\s*[*xX]\s*\d+\s*[*xX]\s*\d+(\s*[A-Z]+)?\b', ' ', s)
    s = re.sub(r'\b\d+\s*[*xX]\s*\d+(\s*[A-Z]+)?\b', ' ', s)
    s = re.sub(r'\b\d+\s*\*\s*\d+\b', ' ', s)
    s = re.sub(r'\b\d+\'S\b', ' ', s)

    # 6. Separate glued packaging, packs, units (avoid splitting X inside packaging)
    s = re.sub(r'([A-WYZ])(\d+X\d+)', r'\1 \2', s)
    s = re.sub(r'([A-Z])(\d+\s*(?:ML|GM|MG|LTR|LT|KG)\b)', r'\1 \2', s)
    s = re.sub(r'(\d+M)(\d+ML)', r'\1 \2', s)
    s = re.sub(r'\b(SUSP|SUSPEN|SUSPENSION|TAB|TABLET|CAP|CAPSU|CAPSUL|CAPSULE|SYP|SYRUP|OINT|CREAM|GEL|INJ|DROPS?|SOFTGEL)(\d+)', r'\1 \2', s)

    # 7. Remove volume/weight quantities e.g. 200ML, 30 ML, 5 LTR, 170M
    s = re.sub(r'\b\d+\s*(?:ML|GM|MG|LTR|LT|KG|M)\b', ' ', s)
    s = re.sub(r'(\d+)(?:ML|GM|MG|LTR|LT|KG|M)\b', ' ', s)

    # 8. Remove packaging prefixes/materials that get truncated: ALU-ALU, ALU, ALUMUNIAM, STRIP, STP, BLISTER
    s = re.sub(r'\b(?:ALU\s*ALU|ALU|AL|ALUMUNIAM|ALUMINIUM|SILVER|GOLD|STRIP|STP|STR|BLISTER|BLIST)\b', ' ', s)

    # 9. Replace non-alphanumeric with spaces
    s = re.sub(r'[^A-Z0-9]', ' ', s)

    # 10. Comprehensive noise words: dosage forms, packaging types, materials, route
    noise = {
        'TAB', 'TABS', 'TABLET', 'TABLETS',
        'CAP', 'CAPS', 'CAPSU', 'CAPSUL', 'CAPSULE', 'CAPSULES', 'SOFTGEL',
        'SYP', 'SYRUP', 'SYRUPS',
        'SUSP', 'SUSPEN', 'SUSPENSION', 'SUS',
        'DROP', 'DROPS', 'DRP',
        'OINT', 'OINTMENT', 'CREAM', 'GEL', 'SOAP',
        'INJ', 'INJECTION', 'LOTION',
        'ORAL', 'SOLUTION', 'SOLN', 'RESPULES', 'RESPULE', 'INHALER',
        'POWDER', 'SACHET', 'SACHETS', 'BOLUS', 'BOLUSES',
        # Packaging types & materials
        'ALU', 'ALUALU', 'ALUMUNIAM', 'ALUMINIUM', 'STRIP', 'STRIPS', 'STR', 'STP',
        'BLISTER', 'BLIST', 'BOTTLE', 'BTL', 'BOX', 'PCS', 'VIAL', 'AMPOULE', 'AMP',
        'SILVER', 'GOLD', 'CONTAINER', 'PACK', 'PKG', 'PK',
        # Units
        'ML', 'GM', 'MG', 'LTR', 'LT', 'KG', 'MCG', 'IU', 'M',
        # General non-distinctive / truncated fragments
        'MRP', 'X'
    }

    tokens = []
    for t in s.split():
        if t in noise:
            continue
        if re.match(r'^\d+X\d*$', t) or re.match(r'^\d+X\d+X\d+$', t) or re.match(r'^\d+X\d+$', t):
            continue
        tokens.append(t)

    # If the last token is just '1' (common MARG pack indicator e.g. 'MRP-150/- 1'), remove it if other tokens exist
    if len(tokens) > 1 and tokens[-1] == '1':
        tokens = tokens[:-1]

    res = ''.join(tokens)
    return res if res else re.sub(r'[^A-Z0-9]', '', str(name)).upper()


def pharma_canonical_key(name: str) -> str:
    """
    Computes a canonical pharmaceutical formulation key.

    Distinguishes true therapeutic variants while grouping formatting/packaging noise:
    - Same drug in same dosage form family with different pack formatting strings
      ('-(10X1) 10X1', '10X1X1 10X1X1', '10*10', '10X1X10') produce identical keys.
    - Preserves distinct dosage forms: 'ALFER-XT SYRUP' != 'ALFER-XT TAB'.
    - Preserves distinct volumes for liquids: 'BEN-CYPO 100ML' != 'BEN-CYPO 200ML'.
    - Preserves active drug combination modifiers like -MR, -S, -PLUS, -FORTE, -D.
    """
    if not name:
        return ''
    s = str(name).strip().upper()

    # 1. Identify dosage form category
    form = 'SOLID'
    if any(k in s for k in ['SYP', 'SYRUP', 'SUSP', 'SUSPEN', 'ORAL SOL', 'DROP', 'DROPS', 'RESPULE', 'SOLUTION', 'SOLN', 'LOTION']):
        form = 'LIQUID'
    elif any(k in s for k in ['INJ', 'INJECTION', 'INFUSION']):
        form = 'INJECTABLE'
    elif any(k in s for k in ['SOAP']):
        form = 'SOAP'
    elif any(k in s for k in ['CREAM', 'OINT', 'OINTMENT', 'GEL']):
        form = 'TOPICAL'
    elif any(k in s for k in ['POWDER', 'SACHET', 'GRANULES']):
        form = 'POWDER'

    # 2. Extract volume / container capacity for liquids and topicals (e.g. 200ML vs 100ML)
    vol = ''
    if form in ('LIQUID', 'TOPICAL', 'SOAP', 'INJECTABLE'):
        m_vol = re.search(r'(\d+)\s*(ML|GM|MG|LTR|LT|KG)\b', s)
        if m_vol:
            vol = f'{m_vol.group(1)}{m_vol.group(2)}'

    # 3. Base canonical medicine name
    base = canonical_medicine_key(name)
    return f'{base}_{form}_{vol}'



def is_footer_or_junk_row(name: str) -> bool:
    """
    Identifies pure footer/header/junk rows (totals, item count banners, empty names).
    Also filters MARG ERP ledger/service charge entries that are not physical products
    (e.g. INVENTORY CHARGES, CYLINDER CHARGE, FREIGHT CHARGES, etc.).
    These entries have billing amounts stored as qty — they must never enter the product catalog.
    """
    if not name or not str(name).strip():
        return True
    s = str(name).strip().upper()
    if re.search(r'^\d+\s*ITEMS?$', s) or 'ITEMS' in s:
        return True
    if s in ('TOTAL', 'GRAND TOTAL', 'SUB TOTAL', 'SUMMARY', 'NAN', 'NONE', 'REMARKS', 'NIL'):
        return True

    # MARG ERP ledger / service charge entries — NOT physical stock items
    # These appear in closing stock reports as billing entries with rupee amounts as qty
    MARG_SERVICE_CHARGE_KEYWORDS = (
        'INVENTORY CHARGE',
        'CYLINDER CHARGE',
        'FREIGHT CHARGE',
        'FREIGHT CHARGES',
        'CARTAGE',
        'LOADING CHARGE',
        'UNLOADING CHARGE',
        'LABOUR CHARGE',
        'SERVICE CHARGE',
        'SERVICE TAX',
        'INTEREST CHARGE',
        'INTEREST ON',
        'LATE PAYMENT',
        'BANK CHARGE',
        'MISC CHARGE',
        'MISCELLANEOUS CHARGE',
        'ROUND OFF',
        'ROUNDING OFF',
        'CASH DISCOUNT',
        'TRADE DISCOUNT',
        'SCHEME DISCOUNT',
        'CLAIM AMOUNT',
        'DAMAGE CLAIM',
        'DEBIT NOTE',
        'CREDIT NOTE',
        'C.S.T.', 'GST CHARGE', 'SGST', 'CGST', 'IGST', 'TCS', 'TDS',
        'OCTROI', 'ENTRY TAX',
        'SAMPLE CHARGE',
        'DEMO CHARGE',
        'HANDLING CHARGE',
    )
    if any(kw in s for kw in MARG_SERVICE_CHARGE_KEYWORDS):
        return True

    return False


JUNK_SUPPLIERS: set[str] = {
    'SUPPLIER NAME', 'SUPPLIER', 'SUPPLIERS', 'PARTY NAME', 'PARTY',
    'MANUFACTURER', 'MFR', 'BILL DATE', 'BILL NO', 'DETAIL', 'TOTAL',
    'GRAND TOTAL', 'SUB TOTAL', 'REMARKS', 'NAN', 'NONE', 'DEFAULT',
    'NEW PARTY', 'PCD', 'NAME OF SUPPLIER', 'NAME OF SUPPLIERS'
}


def sanitize_supplier_name(val: Any) -> str:
    """
    Sanitizes supplier name by stripping leading dates (e.g. '04/08/2026 J.M HEALTHCARE CHANDI'),
    normalizing spacing, and filtering out header artifacts, junk strings, and totals.
    """
    if val is None or pd.isna(val):
        return ''
    s = ' '.join(str(val).split()).strip()
    if not s or s.upper() in JUNK_SUPPLIERS:
        return ''
    if any(bad in s.upper() for bad in ('DETAIL', '====', 'TAX <', 'BILL NO.', 'BILL DATE', '-BLANK-')):
        return ''
    # Strip leading date e.g. 04/08/2026 or 2026-04-08
    s = re.sub(r'^\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}\s*', '', s).strip()
    s = re.sub(r'^\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2}\s*', '', s).strip()
    if not s or s.upper() in JUNK_SUPPLIERS:
        return ''
    if any(bad in s.upper() for bad in ('DETAIL', '====', 'TAX <', 'BILL NO.', 'BILL DATE', '-BLANK-')):
        return ''
    if not re.search(r'[A-Za-z]', s):
        return ''
    return s


def _make_stable_code(name: str) -> str:
    """Generates a stable, deterministic product code from canonical medicine name so stock and sales match 100%."""
    canonical = canonical_medicine_key(name)
    h = int(hashlib.md5(canonical.encode('utf-8')).hexdigest()[:8], 16)
    return f"MED-{h % 100000:05d}"


def _parse_float(val: Any, default: float = 0.0) -> float:
    if val is None or pd.isna(val):
        return default
    s = str(val).replace(',', '').replace('₹', '').replace('Rs.', '').replace('Rs', '').strip()
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def parse_pack_size(val: Any) -> float:
    """
    Parses pharmaceutical pack sizes common in MARG ERP:
    - '1*10' -> 10.0
    - '10*10' -> 100.0
    - '10X10' -> 100.0
    - '10'S' -> 10.0
    - '100 ML' -> 1.0 (or numerical multiplier)
    - 10 -> 10.0
    """
    if val is None or pd.isna(val):
        return 1.0
    s = str(val).strip().upper()
    # Check for expressions like 10*10, 1*10, 10x10
    mult_match = re.search(r'(\d+)\s*[\*xX]\s*(\d+)', s)
    if mult_match:
        try:
            return float(int(mult_match.group(1)) * int(mult_match.group(2)))
        except (ValueError, OverflowError):
            pass
    # Check for 10'S or 100'S
    s_match = re.search(r'(\d+)\s*\'S', s)
    if s_match:
        return float(s_match.group(1))
    # Extract leading number: e.g. '10 TAB', '100 ML', '10'
    num_match = re.search(r'(\d+(\.\d+)?)', s)
    if num_match:
        return max(1.0, float(num_match.group(1)))
    return 1.0


def parse_expiry_date(val: Any) -> datetime | None:
    """
    Parses various date/expiry formats common in MARG ERP:
    - MM/YY, MM/YYYY, MM-YY, MM-YYYY, MM.YY, MM.YYYY (e.g. '04/26', '11/2026')
    - Mon-YY, Mon-YYYY, Mon/YY, Mon/YYYY (e.g. 'Nov-26', 'MAY-2026', 'SEPT-25', 'JULY -2026')
    - YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY
    - Year alone (e.g. '2025', '2026')
    - Pandas Timestamp / datetime object
    In pharmaceutical inventory (FEFO), expiry dates that specify a month and year
    are valid through the LAST DAY of that month.
    """
    if val is None or pd.isna(val):
        return None
    if isinstance(val, (datetime, pd.Timestamp)):
        dt = val.to_pydatetime() if hasattr(val, 'to_pydatetime') else val
        # If it came in as 1st of month (standard Excel auto-parse for MM/YY), adjust to end of month
        if dt.day == 1:
            last_day = calendar.monthrange(dt.year, dt.month)[1]
            return datetime(dt.year, dt.month, last_day, 23, 59, 59)
        return dt

    s = str(val).strip().upper()
    if not s or s in ('NA', 'NULL', 'NONE', '-', '.', 'DEFAULT', '0', '0.0'):
        return None

    # Strip prefixes like EXP:, EXP., EXP, BB:, B.B., E:
    s = re.sub(r'^(?:EXP|EXPDT|EXPIRY|BB|B\.B\.|E)[\s\.:\-_]*', '', s).strip()

    # 0. Format: date-Month-year last two digit (e.g. 15-May-26, 01-Nov-25, 5-AUG-27, 10/Oct/26, 15.11.26, 25-08-26)
    # Day-MonthName-2or4digitYear
    m = re.match(r'^(\d{1,2})[\/\-\.\s]+([A-Za-z]{3,})[\/\-\.\s]+(\d{2,4})$', s)
    if m:
        d, m_str, y_str = int(m.group(1)), m.group(2).upper(), m.group(3)
        month = MONTH_NAME_MAP.get(m_str) or MONTH_NAME_MAP.get(m_str[:3])
        year = int(y_str)
        if year < 100:
            year += 2000
        if month and 1 <= d <= 31 and 2000 <= year <= 2099:
            max_d = calendar.monthrange(year, month)[1]
            return datetime(year, month, min(d, max_d), 23, 59, 59)

    # Day-numericMonth-2digitYear (e.g. 15-05-26, 25/08/26, 01-11-25)
    m = re.match(r'^(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{2})$', s)
    if m:
        d, m_val, y_val = int(m.group(1)), int(m.group(2)), int(m.group(3)) + 2000
        if 1 <= m_val <= 12 and 1 <= d <= 31 and 2000 <= y_val <= 2099:
            max_d = calendar.monthrange(y_val, m_val)[1]
            return datetime(y_val, m_val, min(d, max_d), 23, 59, 59)

    # 1. Full date YYYY-MM-DD
    m = re.match(r'^(20\d{2})[\/\-\.](\d{1,2})[\/\-\.](\d{1,2})', s)
    if m:
        y, m_val, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= m_val <= 12:
            max_d = calendar.monthrange(y, m_val)[1]
            d = min(d, max_d)
            if d == 1:
                d = max_d
            return datetime(y, m_val, d, 23, 59, 59)

    # 2. Full date DD-MM-YYYY or DD/MM/YYYY
    m = re.match(r'^(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](20\d{2})', s)
    if m:
        d, m_val, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= m_val <= 12:
            max_d = calendar.monthrange(y, m_val)[1]
            d = min(d, max_d)
            return datetime(y, m_val, d, 23, 59, 59)

    # 3. Month name and year (e.g. NOV-2025, MAY-26, SEPT-25, JULY -2026, JAN/2026, DEC 2027)
    m = re.search(r'\b(JAN|FEB|MAR|APR|MAY|JUN|JUNE|JUL|JULY|AUG|SEP|SEPT|SEPTEMBER|OCT|NOV|DEC)[A-Z]*\s*[\/\-\.\s]\s*(\d{2,4})\b', s)
    if m:
        m_str, y_str = m.group(1), m.group(2)
        month = MONTH_NAME_MAP.get(m_str)
        year = int(y_str)
        if year < 100:
            year += 2000
        if month and 2000 <= year <= 2099:
            last_day = calendar.monthrange(year, month)[1]
            return datetime(year, month, last_day, 23, 59, 59)

    # 4. MM/YY or MM/YYYY (e.g. 04/26, 11/2025, 4-26, 04.26)
    m = re.match(r'^(\d{1,2})[\/\-\.](\d{2,4})$', s)
    if m:
        m_val = int(m.group(1))
        y_val = int(m.group(2))
        if y_val < 100:
            y_val += 2000
        if 1 <= m_val <= 12 and 2000 <= y_val <= 2099:
            last_day = calendar.monthrange(y_val, m_val)[1]
            return datetime(y_val, m_val, last_day, 23, 59, 59)

    # 5. YYYY/MM (e.g. 2026/04, 2026-04)
    m = re.match(r'^(20\d{2})[\/\-\.](\d{1,2})$', s)
    if m:
        y_val = int(m.group(1))
        m_val = int(m.group(2))
        if 1 <= m_val <= 12:
            last_day = calendar.monthrange(y_val, m_val)[1]
            return datetime(y_val, m_val, last_day, 23, 59, 59)

    # 6. Year alone (e.g. 2025, 2026)
    m = re.match(r'^(20\d{2})$', s)
    if m:
        year = int(m.group(1))
        return datetime(year, 12, 31, 23, 59, 59)

    # 7. Generic fallback
    try:
        dt = pd.to_datetime(s, errors='coerce')
        if pd.notna(dt):
            pdt = dt.to_pydatetime()
            if pdt.day == 1:
                ld = calendar.monthrange(pdt.year, pdt.month)[1]
                return datetime(pdt.year, pdt.month, ld, 23, 59, 59)
            return pdt
    except Exception:
        pass

    return None


PROMO_NAME_KEYWORDS: tuple[str, ...] = (
    'BAG', 'DIARY', 'SHIRT', 'CALENDER', 'CALENDAR', 'FOIL', 'BOX', 'PLAT CHARGES', 'PRODUCT',
    'VISUAL-AID', 'PACKING', 'PEN-', 'PILLOW', 'PAD-', 'BANNER', 'STANDY', 'PAPER WEIGHT', 'CARTON'
)


def is_promotional_material(name: str, has_mfr: bool = False, has_sup: bool = False) -> bool:
    """
    Determines if an item is promotional/packaging material:
    1. Product name contains promotional keywords: BAG, DIARY, SHIRT, CALENDER, CALENDAR, FOIL, BOX, PLAT CHARGES, PRODUCT, VISUAL-AID, PACKING, PEN-, PILLOW, PAD-, BANNER, STANDY, PAPER WEIGHT, CARTON.
    2. Both manufacturer and supplier columns are blank in the input row.
    """
    name_upper = (name or '').upper()
    if any(kw in name_upper for kw in PROMO_NAME_KEYWORDS):
        return True
    return (not has_mfr) and (not has_sup)


def determine_expiry_category(exp_dt: datetime | None, is_promo: bool = False) -> str:
    """
    Populates product category directly based on pharmaceutical expiry date lifecycle.
    - Promo Material: promotional items or items with empty manufacturer and supplier
    - Expired / Phased Out: batch expiry date is in the past
    - Near-Expiry (<180d): batch expiry date is within 180 days
    - Active Shelf-Life: batch expiry date is healthy (> 180 days)
    """
    if is_promo:
        return 'Promo Material'
    if not exp_dt:
        return 'General / No Expiry'
    now_dt = datetime.utcnow()
    if exp_dt < now_dt:
        return 'Expired / Phased Out'
    elif exp_dt <= now_dt + timedelta(days=180):
        return 'Near-Expiry (<180d)'
    else:
        return 'Active Shelf-Life'


def _find_header_row(df_raw: pd.DataFrame) -> int:
    """
    Detects the true table header row in MARG exports that have company metadata in top rows.
    Handles spaced-out text ('P A R T I C U L A R S', 'C L .   S T O C K') by alphanumeric normalization.
    Scans up to row 35 to accommodate detailed letterheads.
    """
    strong_header_keywords = {
        'particulars', 'itemname', 'productname', 'itemdescription', 'itemcode', 'productcode',
        'clstock', 'closingstock', 'batchno', 'expdate', 'expirydate',
        'purrate', 'purchaserate', 'suppliername', 'partyname',
        'totalbillvaleuptodate', 'balanceoutstanding', 'qtysold', 'soldqty',
        'quantity', 'avrate', 'amount'
    }
    general_header_keywords = {
        'code', 'item', 'product', 'batch', 'particulars', 'description', 'stock', 'rate',
        'supplier', 'party', 'mfr', 'manufacturer', 'vendor', 'balance', 'bill', 'qty',
        'pack', 'pkg', 'packing', 'exp', 'expiry', 'name', 'slno', 'crdays', 'mrname',
        'cost', 'unit', 'reorder', 'moq', 'leadtime', 'free', 'net', 'mrp'
    }

    scan_limit = min(35, len(df_raw))
    for idx in range(scan_limit):
        cleaned_row = [_clean_alpha(x) for x in df_raw.iloc[idx].values if pd.notna(x) and _clean_alpha(x)]
        if len(cleaned_row) < 2:
            continue

        # Check for exact or strong matches in row
        if any(kw in cell for kw in strong_header_keywords for cell in cleaned_row):
            return idx

        matches = sum(1 for kw in general_header_keywords if any(kw in cell for cell in cleaned_row))
        if matches >= 2:
            return idx

    return 0


def _map_columns(df: pd.DataFrame) -> dict[str, str]:
    """
    Maps actual dataframe column names to canonical schema fields based on COLUMN_ALIASES,
    collapsing all spaces, dots, and non-alphanumerics.

    Employs a two-phase resolution:
    Phase 1: Exact alias matches for all canonical schema fields.
    Phase 2: Substring matches only for remaining unmapped columns, ensuring specific exact
             aliases (e.g. 'suppliername' -> 'supplier_name') are never preempted by broad
             substring aliases (e.g. 'name' -> 'product_name').
    """
    mapping: dict[str, str] = {}
    normalized_cols = {col: _clean_alpha(col) for col in df.columns}

    # Phase 1: Exact alias matches across all canonical keys
    for canonical, aliases in COLUMN_ALIASES.items():
        if canonical in mapping.values():
            continue
        for alias in aliases:
            for col_name, norm in normalized_cols.items():
                if col_name not in mapping and norm == alias:
                    mapping[col_name] = canonical
                    break
            if canonical in mapping.values():
                break

    # Phase 2: Substring match fallback for remaining unmapped columns
    for canonical, aliases in COLUMN_ALIASES.items():
        if canonical in mapping.values():
            continue
        for alias in aliases:
            # Avoid overly broad substring collisions (minimum 4 characters)
            if len(alias) < 4:
                continue
            for col_name, norm in normalized_cols.items():
                if col_name not in mapping and alias in norm:
                    mapping[col_name] = canonical
                    break
            if canonical in mapping.values():
                break

    return mapping


def _resolve_headers_and_data(df_raw: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """
    Detects table headers in MARG reports, handling multi-tier / split headers
    (such as when Row h has major categories like 'TAX', 'SUPPLIER' and Row h+1 has
    sub-headers like '%', 'AMOUNT BILL NO.', 'BILL DATE', 'SUPPLIER NAME').
    Returns the sliced dataframe and canonical/merged column names.
    """
    h = _find_header_row(df_raw)
    if h + 1 < len(df_raw):
        next_vals = [_clean_alpha(x) for x in df_raw.iloc[h + 1].tolist() if _clean_alpha(x)]
        subheader_kws = {'suppliername', 'billdate', 'billno', 'amountbillno', 'amount', 'tax', 'rate', 'qty'}
        explicit = any(c in ('suppliername', 'billdate', 'billno', 'amountbillno') for c in next_vals)
        matches = sum(1 for c in next_vals if any(kw in c for kw in subheader_kws))
        is_sub = explicit or (matches >= 2 and not any(c.isdigit() for c in next_vals))

        if is_sub:
            merged: list[str] = []
            for col_i in range(df_raw.shape[1]):
                m_val = _clean_str(df_raw.iloc[h, col_i])
                s_val = _clean_str(df_raw.iloc[h + 1, col_i])
                if s_val:
                    if any(dec in m_val for dec in ('<====', '====>', 'DETAIL')) or not m_val:
                        merged.append(s_val)
                    elif s_val in ('%', 'AMOUNT'):
                        merged.append(f'{m_val} {s_val}' if m_val else s_val)
                    elif any(kw in _clean_alpha(s_val) for kw in ('supplier', 'party', 'billdate', 'billno', 'amount', 'tax')):
                        merged.append(s_val)
                    else:
                        merged.append(f'{m_val} {s_val}' if m_val else s_val)
                else:
                    merged.append(m_val)
            df = df_raw.iloc[h + 2:].copy()
            df.columns = merged
            return df, merged

    df = df_raw.iloc[h + 1:].copy()
    df.columns = [str(c) if pd.notna(c) else f"Col_{i}" for i, c in enumerate(df_raw.iloc[h].values)]
    return df, list(df.columns)


class MargExcelParser:
    """
    Parses MARG Excel & CSV files and returns canonical data structures ready for database ingestion.
    """

    @classmethod
    def parse_file(cls, file_content: bytes | str, filename: str = '') -> dict[str, list[dict[str, Any]]]:
        """
        Takes bytes or file path of an Excel/CSV file, reads all sheets,
        and extracts products, inventory_batches, suppliers, and sales_history.
        """
        extracted: dict[str, list[dict[str, Any]]] = {
            'products': [],
            'inventory_batches': [],
            'suppliers': [],
            'sales_history': [],
        }

        excel_file: pd.ExcelFile | None = None
        try:
            if isinstance(file_content, bytes):
                excel_file = pd.ExcelFile(io.BytesIO(file_content))
            else:
                excel_file = pd.ExcelFile(file_content)
        except Exception:
            excel_file = None

        if excel_file is not None:
            for sheet_name in excel_file.sheet_names:
                df_raw = excel_file.parse(sheet_name, header=None)
                cls._process_sheet(df_raw, sheet_name, extracted, filename=filename)
        else:
            try:
                if isinstance(file_content, bytes):
                    df_raw = pd.read_csv(io.BytesIO(file_content), header=None)
                else:
                    df_raw = pd.read_csv(file_content, header=None)
                cls._process_sheet(df_raw, 'CSV_Data', extracted, filename=filename)
            except Exception as exc:
                raise ValueError(f"Unable to parse file as Excel or CSV: {exc}")

        return extracted

    @classmethod
    def _process_sheet(
        cls,
        df_raw: pd.DataFrame,
        sheet_name: str,
        out: dict[str, list[dict[str, Any]]],
        filename: str = ''
    ) -> None:
        """
        Inspect and ingest a single Excel worksheet or CSV table.

        Performs:
        1. Multi-tier header resolution and column normalization.
        2. Report classification (Sales Summary, Purchase Summary, Supplier List, Outstanding, or Stock Status).
        3. Specialized dataset extraction into canonical output dictionary.
        4. Universal supplier extraction safety net for any sheet with a supplier column.

        Args:
            df_raw (pd.DataFrame): Raw un-indexed pandas dataframe.
            sheet_name (str): Worksheet title.
            out (dict[str, list[dict[str, Any]]]): Accumulator dict holding extracted canonical records.
            filename (str): Source export file name for contextual heuristics.
        """
        if df_raw.empty or len(df_raw) < 2:
            return

        # Inspect top 15 rows for MARG report title banner
        banner_text = ' '.join([
            str(x).strip() for row in df_raw.iloc[:min(15, len(df_raw))].values for x in row if pd.notna(x)
        ]).lower()

        file_lower = filename.lower()
        sheet_lower = str(sheet_name).lower()

        df, cols = _resolve_headers_and_data(df_raw)
        df = df.dropna(how='all')
        if df.empty:
            return

        col_map = _map_columns(df)
        df_renamed = df.rename(columns=col_map)
        cleaned_col_names = [_clean_alpha(c) for c in df.columns]

        # Determine report type
        has_stock_cols = any(c in cleaned_col_names for c in [
            'clstock', 'closingstock', 'currentstock', 'currstock', 'stock',
            'batchno', 'bno', 'batch', 'expdate', 'expirydate', 'exp', 'expiry'
        ])

        has_explicit_sales_cols = any(c in cleaned_col_names for c in [
            'qtysold', 'soldqty', 'sold', 'saleqty', 'billedqty', 'saledate', 'billdate'
        ])

        has_general_sales_cols = (
            any(c in cleaned_col_names for c in ['quantity', 'qty']) and
            any(c in cleaned_col_names for c in ['rate', 'avrate', 'amount', 'val', 'value', 'free', 'freeqty']) and
            not has_stock_cols
        )

        is_sales_report = (
            any(kw in banner_text for kw in (
                'sales summary', 'sales report', 'sale report', 'sale summary',
                'sale statement', 'sales statement', 'sale register', 'sales register',
                'item wise sale', 'party wise sale', 'sales analysis', 'sale analysis',
                'sale book', 'sales book', 'daily sale', 'monthly sale'
            )) or
            any(kw in file_lower for kw in ('sales', 'sale', 'billing', 'dispatch')) or
            any(kw in sheet_lower for kw in ('sales', 'sale', 'billing', 'dispatch')) or
            has_explicit_sales_cols or
            has_general_sales_cols or
            ('sale_date' in df_renamed.columns and 'qty_sold' in df_renamed.columns)
        )

        is_purchase_report = (
            ('purchase summary' in banner_text) or
            ('purchase' in file_lower) or
            ('purchase' in sheet_lower)
        )

        is_supplier_sheet = (
            ('manufacturer list' in banner_text) or
            ('supplier list' in banner_text) or
            ('supplier' in file_lower) or
            ('manufacturer' in file_lower) or
            any(w in sheet_lower for w in ('supplier', 'manufacturer', 'mfr', 'vendor')) or
            ('place' in df_renamed.columns and 'remarks' in df_renamed.columns and 'supplier_name' in df_renamed.columns) or
            ('supplier_name' in df_renamed.columns and 'qty_on_hand' not in df_renamed.columns and 'qty_sold' not in df_renamed.columns)
        )

        is_outstanding_sheet = (
            ('outstanding' in banner_text) or
            ('outstanding' in file_lower) or
            ('op master' in sheet_lower) or
            ('totalbillvaleuptodate' in cleaned_col_names)
        )

        if is_sales_report:
            cls._extract_sales_summary(df, out, banner_text)
        elif is_purchase_report:
            cls._extract_purchase_summary(df, out, banner_text)
        elif is_supplier_sheet:
            cls._extract_suppliers(df_renamed, out)
        elif is_outstanding_sheet:
            cls._extract_outstanding(df_renamed, out)
        else:
            cls._extract_stock_and_products(df_renamed, out)

        # Universal supplier extraction safety net:
        # If ANY uploaded sheet contains a column mapped to 'supplier_name', ensure all unique suppliers are added to out['suppliers']
        if 'supplier_name' in df_renamed.columns:
            seen_sids = {s['supplier_id'] for s in out['suppliers']}
            seen_names = {s['supplier_name'].strip().upper() for s in out['suppliers'] if s.get('supplier_name')}
            for raw_val in df_renamed['supplier_name'].dropna():
                clean_sup = sanitize_supplier_name(raw_val)
                if clean_sup and clean_sup.upper() not in seen_names:
                    clean_slug = re.sub(r'[^A-Za-z0-9]', '', clean_sup)[:12].upper()
                    sid = f"SUP-{clean_slug}" if clean_slug else f"SUP-{abs(hash(clean_sup)) % 10000:04d}"
                    if sid not in seen_sids:
                        out['suppliers'].append({
                            'supplier_id': sid,
                            'supplier_name': clean_sup,
                            'contact_name': None,
                            'contact_email': None,
                            'contact_phone': None,
                            'lead_time_days': settings.default_lead_time_days,
                            'min_order_value': 0.0,
                            'reliability_score': 0.90,
                            'is_active': True,
                        })
                        seen_sids.add(sid)
                        seen_names.add(clean_sup.upper())

    @classmethod
    def _extract_sales_summary(
        cls,
        df: pd.DataFrame,
        out: dict[str, list[dict[str, Any]]],
        banner_text: str
    ):
        """
        Extracts sales lines from MARG Sales Summary (e.g. SALES REPORT.XLS).
        Extracts date range, computes daily demand velocity, and logs sales history.
        """
        seen_products: set[str] = {p['product_code'] for p in out['products']}

        # Extract date range from banner (e.g. '01/04/2026 - 24/09/2026')
        period_days = 90
        end_date = datetime.utcnow()
        dates = re.findall(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})', banner_text)
        if len(dates) >= 2:
            for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d'):
                try:
                    d1 = datetime.strptime(dates[0], fmt)
                    d2 = datetime.strptime(dates[1], fmt)
                    days_diff = (d2 - d1).days
                    if days_diff > 0:
                        period_days = days_diff
                        end_date = d2
                        break
                except ValueError:
                    pass

        # Identify columns — handles all known MARG sales report column header variants
        name_col = None
        qty_col = None
        free_col = None
        rate_col = None

        for c in df.columns:
            c_norm = _clean_alpha(c)
            if c_norm in (
                'itemdescription', 'particulars', 'itemname', 'productname', 'item',
                'itemdesc', 'description', 'desc', 'name', 'productdescription',
                'productdesc', 'medicinename', 'drugname', 'medicineitemname',
            ):
                name_col = c
            elif c_norm in ('quantity', 'qtysold', 'qty', 'sold', 'billedqty', 'totalqty', 'saleqty'):
                qty_col = c
            elif c_norm in ('free', 'freeqty', 'freeunit', 'freequantity'):
                free_col = c
            elif c_norm in ('avrate', 'rate', 'cost', 'unitcost', 'averagerate', 'avgrate', 'mrp'):
                rate_col = c

        if not name_col:
            name_col = df.columns[0]
        if not qty_col and len(df.columns) > 1:
            qty_col = df.columns[1]

        for _, row in df.iterrows():
            name = _clean_str(row.get(name_col))
            if not name or is_footer_or_junk_row(name):
                continue

            qty  = _parse_float(row.get(qty_col), 0.0) if qty_col else 0.0
            free = _parse_float(row.get(free_col), 0.0) if free_col else 0.0
            total_sold = qty + free
            rate = _parse_float(row.get(rate_col), 0.0) if rate_col else 0.0

            code = _make_stable_code(name)

            # ONE row per product — no synthetic spreading.
            # qty_sold is normalised to the DemandService lookback window (90 days)
            # so that avg_daily = sum(qty_sold) / 90 returns the correct daily rate.
            #
            # Formula:
            #   daily_rate       = total_sold / period_days
            #   normalised_qty   = daily_rate × 90
            #
            # Example: 240 units sold over 176 days
            #   daily_rate     = 240 / 176  = 1.363 units/day
            #   normalised_qty = 1.363 × 90 = 122.7
            #   DemandService  : 122.7 / 90 = 1.363 ✓
            if total_sold > 0:
                DEMAND_LOOKBACK = 90
                daily_rate = total_sold / max(1.0, float(period_days))
                normalised_qty = round(daily_rate * DEMAND_LOOKBACK, 4)
                out['sales_history'].append({
                    'product_code': code,
                    'product_name': name,       # saved from ITEM DESCRIPTION column
                    'sale_date': end_date,      # single representative date (end of report period)
                    'qty_sold': normalised_qty,
                    'channel': 'retail',
                })

    @classmethod
    def _extract_purchase_summary(
        cls,
        df: pd.DataFrame,
        out: dict[str, list[dict[str, Any]]],
        banner_text: str
    ):
        """
        Extracts purchase data from MARG Purchase Summary (e.g. PURCHASE SUMMARY.XLS).
        Updates products with actual procurement purchase costs.
        """
        seen_products = {p['product_code']: p for p in out['products']}

        name_col = df.columns[0]
        rate_col = None
        for c in df.columns:
            if _clean_alpha(c) in ('avrate', 'rate', 'cost', 'unitcost'):
                rate_col = c
                break

        for _, row in df.iterrows():
            name = _clean_str(row.get(name_col))
            if not name or is_footer_or_junk_row(name):
                continue

            rate = _parse_float(row.get(rate_col), 0.0) if rate_col else 0.0
            code = _make_stable_code(name)

            if code in seen_products:
                if rate > 0:
                    seen_products[code]['unit_cost'] = rate
            else:
                out['products'].append({
                    'product_code': code,
                    'product_name': name,
                    'category': 'Purchased Items',
                    'unit': 'pack',
                    'pack_size': parse_pack_size(name),
                    'min_order_qty': 1.0,
                    'unit_cost': rate,
                    'reorder_point': 0.0,
                    'reorder_enabled': True,
                    'preferred_supplier_id': None,
                })
                seen_products[code] = out['products'][-1]

    @classmethod
    def _extract_suppliers(cls, df: pd.DataFrame, out: dict[str, list[dict[str, Any]]]):
        seen_suppliers: set[str] = {s['supplier_id'] for s in out['suppliers']}
        seen_names: set[str] = {s['supplier_name'].strip().upper() for s in out['suppliers'] if s.get('supplier_name')}

        for idx, row in df.iterrows():
            raw_name = row.get('supplier_name')
            name = sanitize_supplier_name(raw_name)
            if not name or name.upper() in seen_names:
                continue

            sid = _clean_str(row.get('supplier_id'))
            if not sid:
                clean_name_slug = re.sub(r'[^A-Za-z0-9]', '', name)[:12].upper()
                sid = f"SUP-{clean_name_slug}" if clean_name_slug else f"SUP-{abs(hash(name)) % 10000:04d}"

            if sid in seen_suppliers:
                continue

            remarks = _clean_str(row.get('remarks')).upper()
            reliability = 0.90
            if 'WHO' in remarks:
                reliability = 0.98
            elif 'GMP' in remarks:
                reliability = 0.95

            place = _clean_str(row.get('place'))
            cat_type = _clean_str(row.get('category'))
            contact_info = f"{place} ({cat_type})" if place and cat_type else (place or cat_type or None)
            lead_time = int(_parse_float(row.get('lead_time_days'), settings.default_lead_time_days))

            out['suppliers'].append({
                'supplier_id': sid,
                'supplier_name': name,
                'contact_name': contact_info,
                'contact_email': _clean_str(row.get('contact_email')) or None,
                'contact_phone': _clean_str(row.get('contact_phone')) or None,
                'lead_time_days': lead_time,
                'min_order_value': _parse_float(row.get('min_order_value'), 0.0),
                'reliability_score': reliability,
                'is_active': True,
            })
            seen_suppliers.add(sid)
            seen_names.add(name.upper())

    @classmethod
    def _extract_outstanding(cls, df: pd.DataFrame, out: dict[str, list[dict[str, Any]]]):
        seen_suppliers: set[str] = {s['supplier_id'] for s in out['suppliers']}
        seen_products: set[str] = {p['product_code'] for p in out['products']}

        col_date = datetime.utcnow()
        for col in df.columns:
            m = re.search(r'(\d{1,2})[\.\/\-](\d{1,2})[\.\/\-](\d{2,4})', str(col))
            if m:
                try:
                    d, m_val, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    y = 2000 + y if y < 100 else y
                    col_date = datetime(y, m_val, d)
                    break
                except Exception:
                    pass

        for idx, row in df.iterrows():
            party_name = _clean_str(row.get('product_name') or row.get('supplier_name'))
            if not party_name or party_name.upper() in ('TOTAL', 'GRAND TOTAL', 'NAN', 'NONE'):
                continue

            clean_slug = re.sub(r'[^A-Za-z0-9]', '', party_name)[:10].upper()
            party_id = f"PCD-{clean_slug}" if clean_slug else f"PCD-{abs(hash(party_name)) % 10000:04d}"

            cr_days = int(_parse_float(row.get('lead_time_days'), settings.default_lead_time_days))
            mr_name = _clean_str(row.get('channel'))
            total_bill = _parse_float(row.get('qty_sold'), 0.0)
            balance = _parse_float(row.get('balance_outstanding'), 0.0)

            rel_score = 0.90
            if total_bill > 0:
                rel_score = round(max(0.5, min(1.0, 1.0 - (balance / (total_bill + 1e-5)))), 2)

            if party_id not in seen_suppliers:
                out['suppliers'].append({
                    'supplier_id': party_id,
                    'supplier_name': party_name,
                    'contact_name': f"MR: {mr_name}" if mr_name else None,
                    'contact_email': None,
                    'contact_phone': None,
                    'lead_time_days': max(1, cr_days),
                    'min_order_value': 0.0,
                    'reliability_score': rel_score,
                    'is_active': True,
                })
                seen_suppliers.add(party_id)

            if party_id not in seen_products:
                out['products'].append({
                    'product_code': party_id,
                    'product_name': f"{party_name} (PCD Demand)",
                    'category': 'PCD Sales',
                    'unit': 'bill_value',
                    'pack_size': 1.0,
                    'min_order_qty': 1.0,
                    'unit_cost': 1.0,
                    'reorder_point': balance,
                    'reorder_enabled': True,
                    'preferred_supplier_id': party_id,
                })
                seen_products.add(party_id)

            if total_bill > 0:
                out['sales_history'].append({
                    'product_code': party_id,
                    'sale_date': col_date,
                    'qty_sold': total_bill,
                    'channel': mr_name or 'PCD',
                })

    @classmethod
    def _extract_stock_and_products(cls, df: pd.DataFrame, out: dict[str, list[dict[str, Any]]]):
        seen_products: set[str] = {p['product_code'] for p in out['products']}
        seen_suppliers: set[str] = {s['supplier_id'] for s in out['suppliers']}

        for _, row in df.iterrows():
            code = _clean_str(row.get('product_code'))
            name = _clean_str(row.get('product_name'))
            # If item and description were in separate columns (e.g. 'I T E M' and 'D E S C R I P T I O N')
            if 'D E S C R I P T I O N' in row and row['D E S C R I P T I O N']:
                desc_val = _clean_str(row['D E S C R I P T I O N'])
                if desc_val and desc_val not in name:
                    name = f"{name} {desc_val}".strip() if name else desc_val

            if not code and not name:
                continue
            if is_footer_or_junk_row(name):
                continue
            if not code:
                code = _make_stable_code(name)
            if not name:
                name = code

            company = _clean_str(row.get('company')) or _clean_str(row.get('Company')) or _clean_str(row.get('COMPANY'))
            category = _clean_str(row.get('category')) or 'General'
            if not company and category and category != 'General':
                company = category
            if not company:
                company = 'General'
            unit = _clean_str(row.get('unit')) or 'strip'
            pack_size = parse_pack_size(row.get('pack_size') or name)
            min_order_qty = max(1.0, _parse_float(row.get('min_order_qty'), 1.0))
            reorder_point = _parse_float(row.get('reorder_point'), 0.0)

            # Prioritize Cost Price from stock sheet
            cost_val = (
                _parse_float(row.get('unit_cost')) or
                _parse_float(row.get('Cost Price')) or
                _parse_float(row.get('Cost Price ')) or
                _parse_float(row.get('COST PRICE')) or
                _parse_float(row.get('costprice')) or
                _parse_float(row.get('COST')) or
                _parse_float(row.get('Purchase Price')) or
                _parse_float(row.get('pur_rate')) or
                _parse_float(row.get('NET')) or
                _parse_float(row.get('RATE')) or
                _parse_float(row.get('M.R.P.')) or
                0.0
            )

            manufacturer = (
                _clean_str(row.get('manufacturer')) or
                _clean_str(row.get('Manufacturer')) or
                _clean_str(row.get('MANUFACTURER')) or
                _clean_str(row.get('mfr')) or
                _clean_str(row.get('Mfr')) or
                _clean_str(row.get('MFG BY')) or
                _clean_str(row.get('mfg_by')) or
                None
            )

            supplier_name = sanitize_supplier_name(row.get('supplier_name'))
            supplier_id = _clean_str(row.get('supplier_id'))
            if supplier_name and not supplier_id:
                clean_name_slug = re.sub(r'[^A-Za-z0-9]', '', supplier_name)[:12].upper()
                supplier_id = f"SUP-{clean_name_slug}" if clean_name_slug else f"SUP-{abs(hash(supplier_name)) % 10000:04d}"

            if supplier_id and supplier_id not in seen_suppliers and supplier_name:
                out['suppliers'].append({
                    'supplier_id': supplier_id,
                    'supplier_name': supplier_name,
                    'contact_name': None,
                    'contact_email': None,
                    'contact_phone': None,
                    'lead_time_days': int(_parse_float(row.get('lead_time_days'), settings.default_lead_time_days)),
                    'min_order_value': _parse_float(row.get('min_order_value'), 0.0),
                    'reliability_score': 0.90,
                    'is_active': True,
                })
                seen_suppliers.add(supplier_id)

            has_mfr = bool(manufacturer and str(manufacturer).strip() and str(manufacturer).strip().upper() not in ('NAN', 'NONE', '-', '0'))
            has_sup = bool(supplier_name and str(supplier_name).strip() and str(supplier_name).strip().upper() not in ('NAN', 'NONE', '-', '0'))
            is_promo = is_promotional_material(name, has_mfr=has_mfr, has_sup=has_sup)

            # Inventory batch details
            batch_no = _clean_str(row.get('batch_no')) or 'DEFAULT'
            if batch_no != 'DEFAULT':
                batch_no = re.sub(r'^(?:ml|gm|mg|ltr|lt|kg|pcs|tab|cap)(?=[A-Z0-9\-\s])', '', batch_no, flags=re.I).strip() or batch_no
            qty_on_hand = (
                _parse_float(row.get('qty_on_hand')) or
                _parse_float(row.get('Current Stock')) or
                _parse_float(row.get('CURRENT STOCK')) or
                _parse_float(row.get('current_stock')) or
                _parse_float(row.get('Closing Stock')) or
                _parse_float(row.get('CL. STOCK')) or
                0.0
            )
            qty_on_order = _parse_float(row.get('qty_on_order'), 0.0)
            expiry_val = row.get('expiry_date')
            expiry_dt = parse_expiry_date(expiry_val)
            if not expiry_dt and batch_no and batch_no != 'DEFAULT':
                expiry_dt = parse_expiry_date(batch_no)

            # Category populated directly based on pharmaceutical expiry date lifecycle
            cat_expiry = determine_expiry_category(expiry_dt, is_promo=is_promo)
            is_discontinued = (cat_expiry == 'Expired / Phased Out')

            # Every input row is preserved in products table with its specific unit cost and batch details
            out['products'].append({
                'product_code': code,
                'product_name': name,
                'batch_no': batch_no,
                'expiry_date': expiry_dt,
                'category': cat_expiry,
                'company': company,
                'manufacturer': manufacturer,
                'is_promo_material': is_promo,
                'is_stock_row': True,
                'unit': unit,
                'pack_size': pack_size,
                'min_order_qty': min_order_qty,
                'unit_cost': cost_val,
                'current_stock': qty_on_hand,
                'reorder_point': reorder_point,
                'reorder_enabled': (not is_discontinued) and (not is_promo),
                'supplier_name': supplier_name,
                'preferred_supplier_id': supplier_id if (supplier_id and supplier_name) else None,
            })

            out['inventory_batches'].append({
                'product_code': code,
                'batch_no': batch_no,
                'company': company,
                'manufacturer': manufacturer,
                'qty_on_hand': qty_on_hand,
                'qty_on_order': qty_on_order,
                'expiry_date': expiry_dt,
                'unit_cost': cost_val,
            })

    @classmethod
    def _extract_sales(cls, df: pd.DataFrame, out: dict[str, list[dict[str, Any]]]):
        seen_products: set[str] = {p['product_code'] for p in out['products']}

        for _, row in df.iterrows():
            code = _clean_str(row.get('product_code'))
            name = _clean_str(row.get('product_name'))
            if not code and not name:
                continue
            if is_footer_or_junk_row(name):
                continue
            if not code:
                code = _make_stable_code(name)

            date_val = row.get('sale_date')
            sale_dt = parse_expiry_date(date_val) or datetime.utcnow()
            qty = _parse_float(row.get('qty_sold'), 0.0)
            if qty > 0:
                out['sales_history'].append({
                    'product_code': code,
                    'product_name': name or code,
                    'sale_date': sale_dt,
                    'qty_sold': qty,
                    'channel': _clean_str(row.get('channel')) or 'retail',
                })


def create_sample_marg_excel() -> bytes:
    """
    Generates a realistic multi-sheet MARG ERP Excel workbook (.xlsx)
    with Stock Status (FEFO batches, expiry, rates), Sales History, and Suppliers.
    """
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        stock_data = [
            {
                'Item Code': 'MED001',
                'Item Name': 'Paracetamol 500mg Tabs (Strip of 10)',
                'Company': 'Apex Pharma',
                'Packing': '1*10',
                'Batch No': 'AP-8801',
                'Expiry Date': '11/26',
                'Closing Stock': 40,
                'On Order': 0,
                'Pur. Rate': 18.5,
                'Reorder Level': 450,
                'Min Order': 100,
                'Party Name': 'MedPharma Distributors',
            },
            {
                'Item Code': 'MED002',
                'Item Name': 'Amoxicillin 250mg Caps (Strip of 10)',
                'Company': 'Cipla Ltd',
                'Packing': '10*10',
                'Batch No': 'CIP-104',
                'Expiry Date': '08/27',
                'Closing Stock': 250,
                'On Order': 0,
                'Pur. Rate': 45.0,
                'Reorder Level': 200,
                'Min Order': 50,
                'Party Name': 'MedPharma Distributors',
            },
            {
                'Item Code': 'MED003',
                'Item Name': 'Metformin 500mg Tabs (Strip of 10)',
                'Company': 'Sun Pharma',
                'Packing': '10*10',
                'Batch No': 'SUN-551',
                'Expiry Date': '04/26',
                'Closing Stock': 50,
                'On Order': 0,
                'Pur. Rate': 22.0,
                'Reorder Level': 350,
                'Min Order': 100,
                'Party Name': 'HealthPlus Wholesale',
            },
            {
                'Item Code': 'MED006',
                'Item Name': 'Cetirizine 10mg Tabs (Strip of 10)',
                'Company': 'Dr Reddys',
                'Packing': '10*10',
                'Batch No': 'DR-229',
                'Expiry Date': '10/24',
                'Closing Stock': 20,
                'On Order': 0,
                'Pur. Rate': 15.0,
                'Reorder Level': 250,
                'Min Order': 100,
                'Party Name': 'MedPharma Distributors',
            },
            {
                'Item Code': 'MED008',
                'Item Name': 'Azithromycin 500mg Tabs (Strip of 3)',
                'Company': 'Zydus Cadila',
                'Packing': '1*3',
                'Batch No': 'ZY-902',
                'Expiry Date': '12/26',
                'Closing Stock': 12,
                'On Order': 0,
                'Pur. Rate': 120.0,
                'Reorder Level': 80,
                'Min Order': 30,
                'Party Name': 'MedPharma Distributors',
            },
        ]
        df_stock = pd.DataFrame(stock_data)
        df_stock.to_excel(writer, sheet_name='Stock_Status', index=False)

        suppliers_data = [
            {
                'Party Code': 'SUP001',
                'Party Name': 'MedPharma Distributors',
                'Lead Time Days': 5,
                'Min Order Value': 5000,
                'Reliability Score': 0.95,
            },
            {
                'Party Code': 'SUP002',
                'Party Name': 'HealthPlus Wholesale',
                'Lead Time Days': 7,
                'Min Order Value': 2000,
                'Reliability Score': 0.88,
            },
        ]
        df_sup = pd.DataFrame(suppliers_data)
        df_sup.to_excel(writer, sheet_name='Suppliers', index=False)

        base_date = datetime.utcnow()
        sales_data = []
        demand_map = {'MED001': 16, 'MED002': 4, 'MED003': 12, 'MED006': 6, 'MED008': 3}
        for day in range(30, 0, -1):
            s_date = (base_date - timedelta(days=day)).strftime('%d/%m/%Y')
            for pcode, avg in demand_map.items():
                sales_data.append({
                    'Sale Date': s_date,
                    'Item Code': pcode,
                    'Qty Sold': max(1, int(avg + (day % 3) - 1)),
                })
        df_sales = pd.DataFrame(sales_data)
        df_sales.to_excel(writer, sheet_name='Sales_Summary', index=False)

    return output.getvalue()
