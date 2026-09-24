"""
MARG Excel Ingestion Adapter

Parses real-world MARG ERP Excel exports (.xlsx, .xls, .csv) for:
- Closing Stock / Inventory with Batches & Expiry (FEFO)
- Sales History & Billed Outstandings for demand forecasting
- Supplier & Manufacturer master lists

Handles MARG-specific nuances:
- Metadata banner headers before table header
- Column names with spaced-out letters (e.g. 'P A R T I C U L A R S', 'C L .   S T O C K')
- Flexible column naming / fuzzy alias matching with non-alphanumeric collapsing
- Pharmaceutical expiry formats (MM/YY, MM/YYYY, Mon-YY, DD/MM/YYYY)
- Multi-pack formats (1*10, 10'S, 10X10, 100 ML)
- Single-sheet raw supplier exports (e.g. MANUFACTURER LIST.xls)
- PCD Outstanding reports (e.g. outstanding.xlsx)
"""
from __future__ import annotations
import io
import re
from datetime import datetime
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
        'particulars', 'itemname', 'productname', 'itemdescription', 'description',
        'product', 'item', 'itemtitle', 'name', 'product_name'
    ],
    'category': [
        'category', 'company', 'mfg', 'mfgby', 'manufacturer', 'group', 'companyname',
        'brand', 'type'
    ],
    'unit': ['unit', 'uom', 'packingunit'],
    'pack_size': ['packing', 'pack', 'packsize', 'pkg', 'pk', 'packaging', 'pack_size'],
    'min_order_qty': ['moq', 'minorderqty', 'minorder', 'minqty', 'min_order_qty'],
    'reorder_point': [
        'reorderlevel', 'reorderpoint', 'reorderlvl', 'minlevel', 'minstock',
        'orderlevel', 'reorder', 'reorder_point'
    ],
    'unit_cost': [
        'purrate', 'purchaserate', 'prate', 'cost', 'rate', 'unitcost', 'costprice',
        'netrate', 'mrp', 'unit_cost'
    ],
    'batch_no': [
        'batchno', 'bno', 'batch', 'batchnumber', 'lot', 'lotno', 'lotnum', 'batch_no'
    ],
    'expiry_date': [
        'expdate', 'expirydate', 'expiry', 'exp', 'expdt', 'exp_date', 'expiry_date'
    ],
    'qty_on_hand': [
        'clstock', 'closingstock', 'stock', 'balance', 'balqty', 'qty', 'onhand',
        'qtyonhand', 'currstock', 'currentstock', 'qty_on_hand'
    ],
    'qty_on_order': ['qtyonorder', 'onorder', 'pendingpo', 'poqty', 'qty_on_order'],
    'supplier_id': ['supplierid', 'suppliercode', 'partycode', 'vendorid', 'supcode', 'supplier_id'],
    'supplier_name': [
        'suppliername', 'supplier', 'partyname', 'party', 'vendorname', 'vendor',
        'mfr', 'manufacturer', 'supplier_name'
    ],
    'lead_time_days': ['leadtimedays', 'leadtime', 'crdays', 'creditdays', 'lead_time_days'],
    'min_order_value': ['minordervalue', 'mov', 'minorder', 'min_order_value'],
    'sale_date': ['saledate', 'date', 'billdate', 'invoicedate', 'dt', 'sale_date'],
    'qty_sold': [
        'qtysold', 'soldqty', 'sold', 'saleqty', 'billedqty', 'totalbillvaleuptodate',
        'totalbill', 'qty_sold'
    ],
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
    return str(val).strip()


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
    mult_match = re.match(r'^(\d+)\s*[\*xX]\s*(\d+)', s)
    if mult_match:
        try:
            return float(int(mult_match.group(1)) * int(mult_match.group(2)))
        except (ValueError, OverflowError):
            pass
    # Check for 10'S or 100'S
    s_match = re.match(r'^(\d+)\s*\'S', s)
    if s_match:
        return float(s_match.group(1))
    # Extract leading number: e.g. '10 TAB', '100 ML', '10'
    num_match = re.match(r'^(\d+(\.\d+)?)', s)
    if num_match:
        return max(1.0, float(num_match.group(1)))
    return 1.0


def parse_expiry_date(val: Any) -> datetime | None:
    """
    Parses various date/expiry formats common in MARG ERP:
    - MM/YY, MM/YYYY (e.g. '11/26' -> 2026-11-01)
    - MM-YY, MM-YYYY
    - Mon-YY, Mon/YY, Mon-YYYY (e.g. 'Nov-26', 'DEC/2027' -> 2026-11-01)
    - DD/MM/YYYY, YYYY-MM-DD
    - Pandas Timestamp / datetime object
    """
    if val is None or pd.isna(val):
        return None
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.to_pydatetime() if hasattr(val, 'to_pydatetime') else val

    s = str(val).strip()
    if not s or s.lower() in ('na', 'null', 'none', '-', '.'):
        return None

    # Pattern MM/YY or MM-YY (e.g., 08/26, 8/26)
    m = re.match(r'^(\d{1,2})[\/\-](\d{2})$', s)
    if m:
        month = int(m.group(1))
        year = 2000 + int(m.group(2))
        if 1 <= month <= 12:
            return datetime(year, month, 1)

    # Pattern MM/YYYY or MM-YYYY
    m = re.match(r'^(\d{1,2})[\/\-](\d{4})$', s)
    if m:
        month = int(m.group(1))
        year = int(m.group(2))
        if 1 <= month <= 12:
            return datetime(year, month, 1)

    # Month name patterns: Nov-26, NOV/2026
    for fmt in ('%b-%y', '%b/%y', '%b-%Y', '%b/%Y', '%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d', '%d/%m/%y', '%Y/%m/%d'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass

    try:
        dt = pd.to_datetime(s, errors='coerce')
        if pd.notna(dt):
            return dt.to_pydatetime()
    except Exception:
        pass

    return None


def _find_header_row(df_raw: pd.DataFrame) -> int:
    """
    Detects the true table header row in MARG exports that have company metadata in top rows.
    Handles spaced-out text ('P A R T I C U L A R S', 'C L .   S T O C K') by alphanumeric normalization.
    """
    strong_header_keywords = {
        'particulars', 'itemname', 'productname', 'itemcode', 'productcode',
        'clstock', 'closingstock', 'batchno', 'expdate', 'expirydate',
        'purrate', 'purchaserate', 'suppliername', 'partyname',
        'totalbillvaleuptodate', 'balanceoutstanding', 'qtysold', 'soldqty'
    }
    general_header_keywords = {
        'code', 'item', 'product', 'batch', 'particulars', 'description', 'stock', 'rate',
        'supplier', 'party', 'mfr', 'manufacturer', 'vendor', 'balance', 'bill', 'qty',
        'pack', 'pkg', 'packing', 'exp', 'expiry', 'name', 'slno', 'crdays', 'mrname',
        'cost', 'unit', 'reorder', 'moq', 'leadtime'
    }

    for idx in range(min(15, len(df_raw))):
        cleaned_row = [_clean_alpha(x) for x in df_raw.iloc[idx].values if pd.notna(x) and _clean_alpha(x)]
        if len(cleaned_row) < 2:
            # Table column header rows have at least 2 columns; single-cell rows are title banners
            continue

        # Single strong keyword match is sufficient to identify table header
        if any(kw in cell for kw in strong_header_keywords for cell in cleaned_row):
            return idx
        # Multiple general keywords match
        matches = sum(1 for kw in general_header_keywords if any(kw in cell for cell in cleaned_row))
        if matches >= 2:
            return idx

    return 0


def _map_columns(df: pd.DataFrame) -> dict[str, str]:
    """
    Maps actual dataframe column names to canonical schema fields based on COLUMN_ALIASES,
    collapsing all spaces, dots, and non-alphanumerics.
    """
    mapping: dict[str, str] = {}
    normalized_cols = {col: _clean_alpha(col) for col in df.columns}

    for canonical, aliases in COLUMN_ALIASES.items():
        # Exact collapsed match
        for col_name, norm in normalized_cols.items():
            if norm in aliases:
                mapping[col_name] = canonical
                break
        # Substring collapsed match fallback
        if canonical not in mapping.values():
            for col_name, norm in normalized_cols.items():
                if col_name not in mapping:
                    if any(alias in norm for alias in aliases):
                        mapping[col_name] = canonical
                        break
    return mapping


class MargExcelParser:
    """
    Parses MARG Excel & CSV files and returns canonical data structures ready for database ingestion.
    """

    @classmethod
    def parse_file(cls, file_content: bytes | str) -> dict[str, list[dict[str, Any]]]:
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

        # Try loading as Excel workbook
        excel_file: pd.ExcelFile | None = None
        try:
            if isinstance(file_content, bytes):
                excel_file = pd.ExcelFile(io.BytesIO(file_content))
            else:
                excel_file = pd.ExcelFile(file_content)
        except Exception:
            excel_file = None

        if excel_file is not None:
            sheet_names = excel_file.sheet_names
            for sheet_name in sheet_names:
                df_raw = excel_file.parse(sheet_name, header=None)
                cls._process_sheet(df_raw, sheet_name, extracted)
        else:
            # Fallback to CSV parser
            try:
                if isinstance(file_content, bytes):
                    df_raw = pd.read_csv(io.BytesIO(file_content), header=None)
                else:
                    df_raw = pd.read_csv(file_content, header=None)
                cls._process_sheet(df_raw, 'CSV_Data', extracted)
            except Exception as exc:
                raise ValueError(f"Unable to parse file as Excel or CSV: {exc}")

        return extracted

    @classmethod
    def _process_sheet(
        cls,
        df_raw: pd.DataFrame,
        sheet_name: str,
        out: dict[str, list[dict[str, Any]]]
    ):
        if df_raw.empty or len(df_raw) < 2:
            return

        header_idx = _find_header_row(df_raw)
        # Parse data using the detected header row
        df = df_raw.iloc[header_idx + 1:].copy()
        df.columns = df_raw.iloc[header_idx].values
        df = df.dropna(how='all')
        if df.empty:
            return

        sheet_lower = str(sheet_name).lower()
        col_map = _map_columns(df)
        df_renamed = df.rename(columns=col_map)
        cleaned_col_names = [_clean_alpha(c) for c in df.columns]

        # Determine sheet type based on detected columns and sheet name
        is_supplier_sheet = (
            any(w in sheet_lower for w in ('supplier', 'manufacturer', 'mfr', 'vendor')) or
            ('supplier_name' in df_renamed.columns and 'qty_on_hand' not in df_renamed.columns and 'qty_sold' not in df_renamed.columns) or
            ('place' in df_renamed.columns and 'remarks' in df_renamed.columns and 'supplier_name' in df_renamed.columns)
        )

        is_outstanding_sheet = (
            any(w in sheet_lower for w in ('outstanding', 'op master', 'billwise', 'collection')) or
            ('balance_outstanding' in df_renamed.columns or 'totalbillvaleuptodate' in cleaned_col_names)
        )

        is_sales_sheet = (
            'sales' in sheet_lower or
            ('sale_date' in df_renamed.columns and 'qty_sold' in df_renamed.columns)
        )

        if is_supplier_sheet:
            cls._extract_suppliers(df_renamed, out)
        elif is_outstanding_sheet:
            cls._extract_outstanding(df_renamed, out)
        elif is_sales_sheet:
            cls._extract_sales(df_renamed, out)
        else:
            # Default treats sheet as Stock / Inventory (which also defines products and batches)
            cls._extract_stock_and_products(df_renamed, out)

    @classmethod
    def _extract_suppliers(cls, df: pd.DataFrame, out: dict[str, list[dict[str, Any]]]):
        seen_suppliers: set[str] = {s['supplier_id'] for s in out['suppliers']}

        for idx, row in df.iterrows():
            name = _clean_str(row.get('supplier_name'))
            # Filter out section headers or empty rows (e.g. 'PCD', 'NEW PARTY NEED TO VISIT')
            if not name or name.upper() in ('PCD', 'NEW PARTY', 'TOTAL', 'REMARKS', 'NAN', 'NONE'):
                continue

            sid = _clean_str(row.get('supplier_id'))
            if not sid:
                # Generate clean identifier
                clean_name_slug = re.sub(r'[^A-Za-z0-9]', '', name)[:12].upper()
                sid = f"SUP-{clean_name_slug}" if clean_name_slug else f"SUP-{abs(hash(name)) % 10000:04d}"

            if sid in seen_suppliers:
                continue

            # Reliability score bonuses: GMP = 0.95, WHO-GMP = 0.98
            remarks = _clean_str(row.get('remarks')).upper()
            reliability = 0.90
            if 'WHO' in remarks:
                reliability = 0.98
            elif 'GMP' in remarks:
                reliability = 0.95

            # Store location/type in contact_name if available
            place = _clean_str(row.get('place'))
            cat_type = _clean_str(row.get('category'))
            contact_info = f"{place} ({cat_type})" if place and cat_type else (place or cat_type or None)

            lead_time = int(_parse_float(row.get('lead_time_days'), settings.default_lead_time_days))

            out['suppliers'].append({
                'supplier_id': sid,
                'supplier_name': name,
                'contact_name': contact_info,
                'contact_email': None,
                'contact_phone': None,
                'lead_time_days': lead_time,
                'min_order_value': _parse_float(row.get('min_order_value'), 0.0),
                'reliability_score': reliability,
                'is_active': True,
            })
            seen_suppliers.add(sid)

    @classmethod
    def _extract_outstanding(cls, df: pd.DataFrame, out: dict[str, list[dict[str, Any]]]):
        """
        Parses MARG Outstanding sheets (e.g. PCD bill-wise customer & party outstandings).
        Extracts parties as trade partners/suppliers and generates sales history lines.
        """
        seen_suppliers: set[str] = {s['supplier_id'] for s in out['suppliers']}
        seen_products: set[str] = {p['product_code'] for p in out['products']}

        # Check if there is an inspection date in column names (e.g. 'COLLECTION - UPTO.23.09.2026')
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

            # Party ID
            clean_slug = re.sub(r'[^A-Za-z0-9]', '', party_name)[:10].upper()
            party_id = f"PCD-{clean_slug}" if clean_slug else f"PCD-{abs(hash(party_name)) % 10000:04d}"

            # Credit days / Lead time
            cr_days = int(_parse_float(row.get('lead_time_days'), settings.default_lead_time_days))
            mr_name = _clean_str(row.get('channel'))
            total_bill = _parse_float(row.get('qty_sold'), 0.0)
            balance = _parse_float(row.get('balance_outstanding'), 0.0)

            # Reliability score based on balance collection ratio
            rel_score = 0.90
            if total_bill > 0:
                rel_score = round(max(0.5, min(1.0, 1.0 - (balance / (total_bill + 1e-5)))), 2)

            # Register party as trade partner / supplier
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

            # Auto-register product stub for sales demand tracking
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

            # Record sales history line
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
            if not code and not name:
                continue
            if not code:
                # Generate stable code if only name is given
                code = f"MED-{abs(hash(name)) % 100000:05d}"
            if not name:
                name = code

            category = _clean_str(row.get('category')) or 'General'
            unit = _clean_str(row.get('unit')) or 'strip'
            pack_size = parse_pack_size(row.get('pack_size'))
            min_order_qty = max(1.0, _parse_float(row.get('min_order_qty'), 1.0))
            reorder_point = _parse_float(row.get('reorder_point'), 0.0)
            unit_cost = _parse_float(row.get('unit_cost'), 0.0)

            supplier_id = _clean_str(row.get('supplier_id'))
            supplier_name = _clean_str(row.get('supplier_name'))
            if supplier_name and not supplier_id:
                clean_name_slug = re.sub(r'[^A-Za-z0-9]', '', supplier_name)[:12].upper()
                supplier_id = f"SUP-{clean_name_slug}" if clean_name_slug else f"SUP-{abs(hash(supplier_name)) % 10000:04d}"

            # Auto-register supplier if present in stock row
            if supplier_id and supplier_id not in seen_suppliers:
                out['suppliers'].append({
                    'supplier_id': supplier_id,
                    'supplier_name': supplier_name or supplier_id,
                    'contact_name': None,
                    'contact_email': None,
                    'contact_phone': None,
                    'lead_time_days': int(_parse_float(row.get('lead_time_days'), settings.default_lead_time_days)),
                    'min_order_value': _parse_float(row.get('min_order_value'), 0.0),
                    'reliability_score': 0.90,
                    'is_active': True,
                })
                seen_suppliers.add(supplier_id)

            if code not in seen_products:
                out['products'].append({
                    'product_code': code,
                    'product_name': name,
                    'category': category,
                    'unit': unit,
                    'pack_size': pack_size,
                    'min_order_qty': min_order_qty,
                    'unit_cost': unit_cost,
                    'reorder_point': reorder_point,
                    'reorder_enabled': True,
                    'preferred_supplier_id': supplier_id or None,
                })
                seen_products.add(code)

            # Inventory batch
            batch_no = _clean_str(row.get('batch_no')) or 'DEFAULT'
            qty_on_hand = _parse_float(row.get('qty_on_hand'), 0.0)
            qty_on_order = _parse_float(row.get('qty_on_order'), 0.0)
            expiry_val = row.get('expiry_date')
            expiry_dt = parse_expiry_date(expiry_val)

            out['inventory_batches'].append({
                'product_code': code,
                'batch_no': batch_no,
                'qty_on_hand': qty_on_hand,
                'qty_on_order': qty_on_order,
                'expiry_date': expiry_dt,
                'unit_cost': unit_cost,
            })

    @classmethod
    def _extract_sales(cls, df: pd.DataFrame, out: dict[str, list[dict[str, Any]]]):
        seen_products: set[str] = {p['product_code'] for p in out['products']}

        for _, row in df.iterrows():
            code = _clean_str(row.get('product_code'))
            name = _clean_str(row.get('product_name'))
            if not code and not name:
                continue
            if not code:
                code = f"MED-{abs(hash(name)) % 100000:05d}"

            # Auto-register product stub if not already present
            if code not in seen_products:
                out['products'].append({
                    'product_code': code,
                    'product_name': name or code,
                    'category': 'Sales Import',
                    'unit': 'unit',
                    'pack_size': 1.0,
                    'min_order_qty': 1.0,
                    'unit_cost': 0.0,
                    'reorder_point': 0.0,
                    'reorder_enabled': True,
                    'preferred_supplier_id': None,
                })
                seen_products.add(code)

            date_val = row.get('sale_date')
            sale_dt = parse_expiry_date(date_val) or datetime.utcnow()
            qty = _parse_float(row.get('qty_sold'), 0.0)
            if qty > 0:
                out['sales_history'].append({
                    'product_code': code,
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
        # Sheet 1: Stock Status with Batches & Expiry (Standard MARG layout)
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
                'Expiry Date': '10/24',  # Expired / near-expiry to test FEFO guardrail!
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

        # Sheet 2: Suppliers Master
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

        # Sheet 3: Sales History (recent 30 days daily sales sample)
        from datetime import timedelta
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
