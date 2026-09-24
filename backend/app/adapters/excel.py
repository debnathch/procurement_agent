"""
MARG Excel Ingestion Adapter

Parses real-world MARG ERP Excel exports (.xlsx, .xls) for:
- Closing Stock / Inventory with Batches & Expiry (FEFO)
- Sales History for demand forecasting
- Supplier master lists

Handles MARG-specific nuances:
- Metadata banner headers before table header
- Pharmaceutical expiry formats (MM/YY, MM/YYYY, DD/MM/YYYY)
- Flexible column naming / fuzzy alias matching
- Pack size and rate extraction
"""
from __future__ import annotations
import io
import re
from datetime import datetime
from typing import Any
import pandas as pd
from backend.app.core.config import settings


# Column alias dictionary for MARG reports
COLUMN_ALIASES: dict[str, list[str]] = {
    'product_code': [
        'item code', 'itemcode', 'code', 'pro code', 'pro_code', 'item_code',
        'product code', 'pcode', 'item no', 'pro_n', 'pro.code', 'product_code'
    ],
    'product_name': [
        'item name', 'itemname', 'product name', 'particulars', 'description',
        'item description', 'name', 'item', 'product_name', 'product'
    ],
    'category': [
        'company', 'category', 'mfg', 'mfg by', 'manufacturer', 'group', 'company name'
    ],
    'unit': ['unit', 'uom', 'packing unit'],
    'pack_size': ['packing', 'pack', 'pack size', 'pack_size', 'pkg', 'pk'],
    'min_order_qty': ['moq', 'min order qty', 'min order', 'min qty', 'min_order_qty'],
    'reorder_point': [
        'reorder lvl', 'reorder level', 'reorder point', 'min level', 'min stock',
        'order level', 'reorder_point', 're-order'
    ],
    'unit_cost': [
        'pur rate', 'p.rate', 'p rate', 'purchase rate', 'cost', 'rate', 'unit cost',
        'pur.rate', 'unit_cost', 'cost price', 'net rate'
    ],
    'batch_no': [
        'batch', 'batch no', 'batch_no', 'batch number', 'b.no', 'bno', 'lot', 'lot no'
    ],
    'expiry_date': [
        'expiry', 'exp', 'exp date', 'exp. date', 'exp_date', 'expiry date', 'exp dt'
    ],
    'qty_on_hand': [
        'stock', 'closing stock', 'closing', 'balance', 'bal qty', 'qty', 'on hand',
        'qty on hand', 'curr stock', 'current stock', 'qty_on_hand', 'cl. stock'
    ],
    'qty_on_order': ['on order', 'qty on order', 'pending po', 'po qty', 'qty_on_order'],
    'supplier_id': [
        'supplier id', 'supplier code', 'party code', 'party_code', 'vendor id', 'sup code',
        'supplier_id'
    ],
    'supplier_name': [
        'supplier name', 'supplier', 'party', 'party name', 'vendor name', 'vendor',
        'supplier_name'
    ],
    # Sales history specific
    'sale_date': ['date', 'bill date', 'sale date', 'invoice date', 'sale_date', 'dt'],
    'qty_sold': ['qty sold', 'sold qty', 'sold', 'sale qty', 'qty_sold', 'billed qty'],
}


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


def parse_expiry_date(val: Any) -> datetime | None:
    """
    Parses various date/expiry formats common in MARG ERP:
    - MM/YY, MM/YYYY (e.g. '12/26' -> 2026-12-01)
    - MM-YY, MM-YYYY
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

    # General date parsing
    for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d', '%d/%m/%y', '%Y/%m/%d'):
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
    """
    header_keywords = {'code', 'item', 'product', 'batch', 'particulars', 'description', 'stock', 'rate'}
    for idx in range(min(15, len(df_raw))):
        row_values = [str(x).lower().strip() for x in df_raw.iloc[idx].values if pd.notna(x)]
        # Check how many keywords match
        matches = sum(1 for kw in header_keywords if any(kw in cell for cell in row_values))
        if matches >= 2:
            return idx
    return 0


def _map_columns(df: pd.DataFrame) -> dict[str, str]:
    """
    Maps actual dataframe column names to canonical schema fields based on COLUMN_ALIASES.
    """
    mapping: dict[str, str] = {}
    normalized_cols = {col: re.sub(r'[^a-z0-9]', ' ', str(col).lower()).strip() for col in df.columns}

    for canonical, aliases in COLUMN_ALIASES.items():
        for col_name, norm in normalized_cols.items():
            if norm in aliases or any(alias == norm for alias in aliases):
                mapping[col_name] = canonical
                break
        if canonical not in mapping.values():
            # Substring fallback
            for col_name, norm in normalized_cols.items():
                if col_name not in mapping:
                    if any(f' {alias} ' in f' {norm} ' for alias in aliases):
                        mapping[col_name] = canonical
                        break
    return mapping


class MargExcelParser:
    """
    Parses MARG Excel files and returns canonical data structures ready for database ingestion.
    """

    @classmethod
    def parse_file(cls, file_content: bytes | str) -> dict[str, list[dict[str, Any]]]:
        """
        Takes bytes or file path of an Excel file, reads all sheets,
        and extracts products, inventory_batches, suppliers, and sales_history.
        """
        if isinstance(file_content, bytes):
            excel_file = pd.ExcelFile(io.BytesIO(file_content))
        else:
            excel_file = pd.ExcelFile(file_content)

        extracted: dict[str, list[dict[str, Any]]] = {
            'products': [],
            'inventory_batches': [],
            'suppliers': [],
            'sales_history': [],
        }

        # Analyze each sheet
        for sheet_name in excel_file.sheet_names:
            sheet_lower = sheet_name.lower()
            df_raw = excel_file.parse(sheet_name, header=None)
            if df_raw.empty or len(df_raw) < 2:
                continue

            header_idx = _find_header_row(df_raw)
            df = excel_file.parse(sheet_name, skiprows=header_idx)
            df = df.dropna(how='all')
            if df.empty:
                continue

            col_map = _map_columns(df)
            df_renamed = df.rename(columns=col_map)

            # Determine sheet type
            is_sales_sheet = 'sales' in sheet_lower or ('sale_date' in df_renamed.columns and 'qty_sold' in df_renamed.columns)
            is_supplier_sheet = 'supplier' in sheet_lower or 'party' in sheet_lower and ('lead_time_days' in df_renamed.columns or 'min_order_value' in df_renamed.columns)

            if is_sales_sheet:
                cls._extract_sales(df_renamed, extracted)
            elif is_supplier_sheet:
                cls._extract_suppliers(df_renamed, extracted)
            else:
                # Default treats sheet as Stock / Inventory (which also defines products and batches)
                cls._extract_stock_and_products(df_renamed, extracted)

        return extracted

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
                code = f"PROD-{abs(hash(name)) % 100000:05d}"
            if not name:
                name = code

            category = _clean_str(row.get('category')) or 'General'
            unit = _clean_str(row.get('unit')) or 'strip'
            pack_size = max(1.0, _parse_float(row.get('pack_size'), 1.0))
            min_order_qty = max(1.0, _parse_float(row.get('min_order_qty'), 1.0))
            reorder_point = _parse_float(row.get('reorder_point'), 0.0)
            unit_cost = _parse_float(row.get('unit_cost'), 0.0)

            supplier_id = _clean_str(row.get('supplier_id'))
            supplier_name = _clean_str(row.get('supplier_name'))
            if supplier_name and not supplier_id:
                supplier_id = f"SUP-{abs(hash(supplier_name)) % 10000:04d}"

            # Auto-register supplier if present
            if supplier_id and supplier_id not in seen_suppliers:
                out['suppliers'].append({
                    'supplier_id': supplier_id,
                    'supplier_name': supplier_name or supplier_id,
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
        for _, row in df.iterrows():
            code = _clean_str(row.get('product_code'))
            if not code:
                continue
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

    @classmethod
    def _extract_suppliers(cls, df: pd.DataFrame, out: dict[str, list[dict[str, Any]]]):
        seen: set[str] = {s['supplier_id'] for s in out['suppliers']}
        for _, row in df.iterrows():
            sid = _clean_str(row.get('supplier_id'))
            name = _clean_str(row.get('supplier_name'))
            if not sid and not name:
                continue
            if not sid:
                sid = f"SUP-{abs(hash(name)) % 10000:04d}"
            if sid not in seen:
                out['suppliers'].append({
                    'supplier_id': sid,
                    'supplier_name': name or sid,
                    'lead_time_days': int(_parse_float(row.get('lead_time_days'), settings.default_lead_time_days)),
                    'min_order_value': _parse_float(row.get('min_order_value'), 0.0),
                    'reliability_score': _parse_float(row.get('reliability_score'), 0.90),
                    'is_active': True,
                })
                seen.add(sid)


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
                'Packing': 10,
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
                'Packing': 10,
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
                'Packing': 10,
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
                'Packing': 10,
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
                'Packing': 3,
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
        df_stock.to_sheet = df_stock.to_excel(writer, sheet_name='Stock_Status', index=False)

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
