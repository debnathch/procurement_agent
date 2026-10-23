"""
FastAPI application entry point for the MARG Procurement Agent.

Startup: creates all SQLite tables automatically (no manual DB setup needed).
"""
import logging
from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta

from backend.app.core.config import settings
from backend.app.core.database import get_db, init_db
from backend.app.models.entities import (
    ProcurementProposal, ProcurementRun, Product, Supplier,
    InventoryBatch, SalesHistory, AuditEvent, FeedbackEvent,
)
from backend.app.agent.procurement_agent import ProcurementAgent, is_ignored_item_name
from backend.app.services.feedback import ProposalService
from backend.app.services.ingestion import IngestionService
from backend.app.services.sales_intelligence import SalesIntelligenceService
from backend.app.adapters.excel import create_sample_marg_excel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("procurement_agent")

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
app = FastAPI(
    title=settings.app_name,
    version='1.1.0',
    description='Local-first, human-approved procurement agent for pharmaceutical distribution.',
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


@app.on_event('startup')
def on_startup():
    """Create all DB tables on startup (idempotent)."""
    init_db()


# ---------------------------------------------------------------------------
# Root — redirect to interactive API docs
# ---------------------------------------------------------------------------
@app.get('/', include_in_schema=False)
def root():
    return RedirectResponse(url='/docs')


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get('/health', tags=['system'])
def health():
    import os, sys
    is_cloud = bool(
        os.environ.get("RENDER")
        or os.environ.get("DOCKER_RUNTIME")
        or (sys.platform.startswith("linux") and not os.environ.get("DISPLAY"))
    )
    return {
        'status': 'ok',
        'app': settings.app_name,
        'mode': settings.execution_mode,
        'is_cloud': is_cloud,
    }


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------
@app.get('/products', tags=['products'])
def list_products(db: Session = Depends(get_db)):
    """
    Retrieve all registered canonical products from the database.

    Returns:
        list[dict]: List of product metadata dictionaries.
    """
    products = db.scalars(select(Product)).all()
    return [_product_dict(p) for p in products]


@app.get('/products/{product_code}', tags=['products'])
def get_product(product_code: str, db: Session = Depends(get_db)):
    """
    Fetch a single product record by its unique product code.

    Args:
        product_code (str): Unique canonical product code identifier.
        db (Session): Database session.

    Returns:
        dict: Product details.

    Raises:
        HTTPException: 404 if the product is not found.
    """
    p = db.scalars(select(Product).where(Product.product_code == product_code)).first()
    if not p:
        raise HTTPException(status_code=404, detail='Product not found.')
    return _product_dict(p)


def _product_dict(p: Product) -> dict:
    """
    Convert a Product ORM entity into a JSON-serializable dictionary.

    Args:
        p (Product): Product entity instance.

    Returns:
        dict: Serialized product fields.
    """
    return {
        'product_code': p.product_code,
        'product_name': p.product_name,
        'category': p.category,
        'company': p.company or 'General',
        'manufacturer': p.manufacturer or 'General',
        'is_promo_material': bool(getattr(p, 'is_promo_material', False)),
        'unit': p.unit,
        'pack_size': p.pack_size,
        'min_order_qty': p.min_order_qty,
        'unit_cost': p.unit_cost,
        'reorder_enabled': p.reorder_enabled,
        'preferred_supplier_id': p.preferred_supplier_id,
    }


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------
@app.get('/suppliers', tags=['suppliers'])
def list_suppliers(db: Session = Depends(get_db)):
    """
    Retrieve all vendors and suppliers saved in the database.

    Returns:
        list[dict]: List of serialized supplier objects.
    """
    suppliers = db.scalars(select(Supplier)).all()
    return [_supplier_dict(s) for s in suppliers]


def _supplier_dict(s: Supplier) -> dict:
    """
    Convert a Supplier ORM entity into a JSON-serializable dictionary.

    Args:
        s (Supplier): Supplier entity instance.

    Returns:
        dict: Serialized supplier fields.
    """
    return {
        'supplier_id': s.supplier_id,
        'supplier_name': s.supplier_name,
        'lead_time_days': s.lead_time_days,
        'min_order_value': s.min_order_value,
        'reliability_score': s.reliability_score,
        'is_active': s.is_active,
    }


# ---------------------------------------------------------------------------
# Manufacturers
# ---------------------------------------------------------------------------
@app.get('/manufacturers', tags=['products'])
def list_manufacturers(db: Session = Depends(get_db)):
    """Retrieve distinct list of medicine manufacturers saved in the database."""
    mfrs = db.scalars(
        select(Product.manufacturer)
        .where(Product.manufacturer.isnot(None))
        .distinct()
    ).all()
    clean = sorted(list({m.strip() for m in mfrs if m and m.strip() and m.strip().upper() not in ('NONE', 'GENERAL', 'NAN', '-')}))
    return clean


# ---------------------------------------------------------------------------
# Agent — run procurement
# ---------------------------------------------------------------------------
class RunRequest(BaseModel):
    product_codes: Optional[list[str]] = None
    lead_time_days: Optional[int] = None
    review_days: Optional[int] = None
    safety_days: Optional[int] = None
    manufacturer: Optional[str] = None


@app.post('/runs', tags=['agent'], status_code=status.HTTP_201_CREATED)
def trigger_run(
    req: RunRequest = RunRequest(),
    lead_time_days: Optional[int] = Query(None),
    review_days: Optional[int] = Query(None),
    safety_days: Optional[int] = Query(None),
    manufacturer: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Trigger a procurement agent run with configurable policy and manufacturer parameters."""
    eff_lead = req.lead_time_days if req.lead_time_days is not None else (lead_time_days if isinstance(lead_time_days, int) else None)
    eff_review = req.review_days if req.review_days is not None else (review_days if isinstance(review_days, int) else None)
    eff_safety = req.safety_days if req.safety_days is not None else (safety_days if isinstance(safety_days, int) else None)
    eff_mfr = req.manufacturer if req.manufacturer is not None else (manufacturer if isinstance(manufacturer, str) else None)

    agent = ProcurementAgent(db)
    run_id, proposals = agent.run(
        product_codes=req.product_codes,
        lead_time_override=eff_lead,
        review_days_override=eff_review,
        safety_days_override=eff_safety,
        manufacturer_override=eff_mfr,
    )
    return {
        'run_id': run_id,
        'proposals': len(proposals),
        'lead_time_days': eff_lead or settings.default_lead_time_days,
        'review_days': eff_review or settings.default_review_days,
        'safety_days': eff_safety or settings.default_safety_days,
        'manufacturer': eff_mfr or 'All Manufacturers',
    }


@app.get('/runs', tags=['agent'])
def list_runs(db: Session = Depends(get_db)):
    """
    List all historical procurement agent runs ordered by recency.

    Returns:
        list[dict]: List of run summaries including run_id, status, and proposal counts.
    """
    runs = db.scalars(select(ProcurementRun).order_by(ProcurementRun.started_at.desc())).all()
    return [
        {
            'run_id': r.run_id,
            'status': r.status,
            'product_count': r.product_count,
            'proposal_count': r.proposal_count,
            'started_at': r.started_at.isoformat() if r.started_at else None,
        }
        for r in runs
    ]

@app.get('/procurement/no-reorder', tags=['agent'])
def list_no_reorder_products(
    lead_time_days: Optional[int] = None,
    review_days: Optional[int] = None,
    safety_days: Optional[int] = None,
    search: Optional[str] = None,
    company: Optional[str] = None,
    manufacturer: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """
    Returns active products with Net Need < 0, ordered alphabetically by character.
    Supports optional search, company, manufacturer, and coverage policy filtering.
    """
    agent = ProcurementAgent(db)
    items = agent.get_no_reorder_products(
        lead_time_override=lead_time_days,
        review_days_override=review_days,
        safety_days_override=safety_days,
        manufacturer_override=manufacturer,
    )
    if company and company.lower() != 'all companies':
        items = [i for i in items if (i.get('company') or '').lower() == company.lower()]
    if manufacturer and manufacturer.lower() != 'all manufacturers':
        items = [i for i in items if (i.get('manufacturer') or '').lower() == manufacturer.lower()]
    if search:
        s_low = search.strip().lower()
        items = [
            i for i in items
            if s_low in i['product_name'].lower() or s_low in i['product_code'].lower()
        ]
    return items

# ---------------------------------------------------------------------------
@app.get('/proposals', tags=['proposals'])
def list_proposals(
    status_filter: Optional[str] = None,
    company: Optional[str] = None,
    manufacturer: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """
    Retrieve procurement proposals optionally filtered by lifecycle status, company, and manufacturer.

    Args:
        status_filter (Optional[str]): Optional filter (e.g. 'PENDING', 'APPROVED', 'REJECTED').
        company (Optional[str]): Optional company filter.
        manufacturer (Optional[str]): Optional manufacturer filter.
        db (Session): Database session.

    Returns:
        list[dict]: List of proposal records ordered alphabetically by product name.
    """
    stmt = select(ProcurementProposal).order_by(ProcurementProposal.product_name.asc())
    if status_filter:
        stmt = stmt.where(ProcurementProposal.status == status_filter.upper())
    if company and company.lower() != 'all companies':
        stmt = stmt.where(ProcurementProposal.company == company)
    if manufacturer and manufacturer.lower() != 'all manufacturers':
        stmt = stmt.where(ProcurementProposal.manufacturer == manufacturer)
    proposals = db.scalars(stmt).all()
    return [_proposal_dict(p) for p in proposals]


# ---------------------------------------------------------------------------
# Agentic Ecosystem APIs — Procurement Recommendations by Category & Division
# ---------------------------------------------------------------------------

class OrderItemResponse(BaseModel):
    proposal_id: int
    product_code: str
    product_name: str
    division: str
    company: str
    category: str
    manufacturer: str
    supplier_id: Optional[str] = None
    supplier_name: Optional[str] = None
    recommended_qty: float
    approved_qty: Optional[float] = None
    unit_cost: float
    estimated_value: float
    avg_daily_demand: float
    lead_time_days: int
    stock_on_hand: float
    stock_on_order: float
    order_urgency: str
    order_qty_flag: str
    near_expiry_qty: float
    expiry_risk_score: float
    expiry_action: Optional[str] = None
    rationale: Optional[str] = None
    status: str
    created_at: Optional[str] = None


class DivisionSummary(BaseModel):
    division: str
    product_count: int
    total_units: float
    total_value: float


class CategorySummary(BaseModel):
    category: str
    product_count: int
    total_units: float
    total_value: float


class OrdersByCategoryDivisionResponse(BaseModel):
    status: str
    metadata: dict
    summary_by_division: list[DivisionSummary]
    summary_by_category: list[CategorySummary]
    items: list[OrderItemResponse]


def _resolve_param(val, default=None):
    if val is None:
        return default
    if isinstance(val, (str, int, float, bool)):
        return val
    if hasattr(val, 'default'):
        return val.default if val.default is not ... else default
    return default


def _get_product_category_map(db: Session) -> dict[str, str]:
    """Build a mapping of product_code to category."""
    rows = db.execute(
        select(Product.product_code, Product.category)
        .where(Product.category.is_not(None))
        .group_by(Product.product_code)
    ).all()
    return {r[0]: (r[1] or 'General / Active Shelf-Life') for r in rows}


@app.get(
    '/procurement/orders-by-category-division',
    response_model=OrdersByCategoryDivisionResponse,
    tags=['agent-procurement'],
    summary="Agent API: Get products to order filtered by Category and Division"
)
def get_orders_by_category_and_division(
    division: Optional[str] = Query(None, description="Filter by marketing division / company (e.g. 'BENGAL REMEDIES', 'GENIAL LIFECARE')"),
    category: Optional[str] = Query(None, description="Filter by product category (e.g. 'Active Shelf-Life', 'General / No Expiry')"),
    manufacturer: Optional[str] = Query(None, description="Filter by manufacturer unit (e.g. 'JMH', 'ORISON')"),
    status: Optional[str] = Query('PENDING', description="Filter by proposal lifecycle status: 'PENDING', 'APPROVED_PENDING_EXECUTION', 'EXECUTED', 'REJECTED', or 'ALL'"),
    urgency: Optional[str] = Query(None, description="Filter by urgency: 'high' (> 100 units) or 'normal' (<= 100 units)"),
    min_recommended_qty: Optional[float] = Query(None, description="Minimum recommended order quantity threshold"),
    min_order_value: Optional[float] = Query(None, description="Minimum estimated purchase order value in INR"),
    search: Optional[str] = Query(None, description="Search keyword for product name or product code"),
    sort_by: str = Query('estimated_value', description="Sort field: 'estimated_value', 'recommended_qty', 'product_name', 'avg_daily_demand'"),
    sort_order: str = Query('desc', description="Sort direction: 'desc' or 'asc'"),
    limit: int = Query(100, ge=1, le=1000, description="Pagination limit (max 1000)"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    db: Session = Depends(get_db),
):
    """
    Standardized procurement intelligence API designed for consumption by autonomous AI agents.
    
    Enables autonomous agents in an agentic ecosystem to query exactly which products to reorder,
    sliced by pharmaceutical marketing division and product category, with complete financial and
    inventory positioning metrics.
    """
    status = _resolve_param(status, 'PENDING')
    division = _resolve_param(division, None)
    category = _resolve_param(category, None)
    manufacturer = _resolve_param(manufacturer, None)
    urgency = _resolve_param(urgency, None)
    min_recommended_qty = _resolve_param(min_recommended_qty, None)
    min_order_value = _resolve_param(min_order_value, None)
    search = _resolve_param(search, None)
    sort_by = _resolve_param(sort_by, 'estimated_value')
    sort_order = _resolve_param(sort_order, 'desc')
    limit = _resolve_param(limit, 100)
    offset = _resolve_param(offset, 0)

    code_to_cat = _get_product_category_map(db)

    stmt = select(ProcurementProposal)
    if status and status.upper() != 'ALL':
        stmt = stmt.where(ProcurementProposal.status == status.upper())
    
    proposals = db.scalars(stmt).all()

    # Enriched in-memory filtering
    filtered_items: list[dict] = []
    for p in proposals:
        if is_ignored_item_name(p.product_name):
            continue

        p_div = p.company or 'General'
        p_cat = code_to_cat.get(p.product_code, 'General / Active Shelf-Life')
        p_mfr = p.manufacturer or 'General'

        # Filter: Division / Company (case-insensitive substring)
        if division and division.strip().lower() != 'all':
            if division.strip().lower() not in p_div.lower():
                continue

        # Filter: Category (case-insensitive substring)
        if category and category.strip().lower() != 'all':
            if category.strip().lower() not in p_cat.lower():
                continue

        # Filter: Manufacturer
        if manufacturer and manufacturer.strip().lower() != 'all':
            if manufacturer.strip().lower() not in p_mfr.lower():
                continue

        # Filter: Urgency
        if urgency:
            u_clean = urgency.strip().lower()
            if u_clean == 'high' and p.recommended_qty <= 100:
                continue
            elif u_clean == 'normal' and (p.recommended_qty <= 0 or p.recommended_qty > 100):
                continue

        # Filter: Min recommended qty
        if min_recommended_qty is not None and p.recommended_qty < min_recommended_qty:
            continue

        # Filter: Min order value
        if min_order_value is not None and p.estimated_value < min_order_value:
            continue

        # Filter: Keyword search
        if search:
            s_clean = search.strip().lower()
            if s_clean not in p.product_name.lower() and s_clean not in p.product_code.lower():
                continue

        urgency_label = "HIGH" if p.recommended_qty > 100 else "NORMAL"
        qty_flag = "🚩 > 100 (High Qty)" if p.recommended_qty > 100 else "🟡 ≤ 100 (Normal Qty)"

        filtered_items.append({
            'proposal_id': p.id,
            'product_code': p.product_code,
            'product_name': p.product_name,
            'division': p_div,
            'company': p_div,
            'category': p_cat,
            'manufacturer': p_mfr,
            'supplier_id': p.supplier_id,
            'supplier_name': p.supplier_name or 'Default Supplier',
            'recommended_qty': p.recommended_qty,
            'approved_qty': p.approved_qty,
            'unit_cost': p.unit_cost,
            'estimated_value': p.estimated_value,
            'avg_daily_demand': round(p.avg_daily_demand, 4),
            'lead_time_days': p.lead_time_days,
            'stock_on_hand': p.stock_on_hand,
            'stock_on_order': p.stock_on_order,
            'order_urgency': urgency_label,
            'order_qty_flag': qty_flag,
            'near_expiry_qty': p.near_expiry_qty,
            'expiry_risk_score': p.expiry_risk_score,
            'expiry_action': p.expiry_action,
            'rationale': p.rationale,
            'status': p.status,
            'created_at': p.created_at.isoformat() if p.created_at else None,
        })

    # Aggregations across all matching items
    total_matching = len(filtered_items)
    total_units = sum(i['recommended_qty'] for i in filtered_items)
    total_value = sum(i['estimated_value'] for i in filtered_items)

    div_agg = defaultdict(lambda: {'count': 0, 'units': 0.0, 'value': 0.0})
    cat_agg = defaultdict(lambda: {'count': 0, 'units': 0.0, 'value': 0.0})

    for item in filtered_items:
        d = item['division']
        c = item['category']
        div_agg[d]['count'] += 1
        div_agg[d]['units'] += item['recommended_qty']
        div_agg[d]['value'] += item['estimated_value']
        cat_agg[c]['count'] += 1
        cat_agg[c]['units'] += item['recommended_qty']
        cat_agg[c]['value'] += item['estimated_value']

    summary_by_division = [
        DivisionSummary(
            division=k,
            product_count=v['count'],
            total_units=round(v['units'], 2),
            total_value=round(v['value'], 2)
        )
        for k, v in sorted(div_agg.items(), key=lambda x: x[1]['value'], reverse=True)
    ]

    summary_by_category = [
        CategorySummary(
            category=k,
            product_count=v['count'],
            total_units=round(v['units'], 2),
            total_value=round(v['value'], 2)
        )
        for k, v in sorted(cat_agg.items(), key=lambda x: x[1]['value'], reverse=True)
    ]

    # Sorting
    reverse = (sort_order.lower() == 'desc')
    if sort_by == 'recommended_qty':
        filtered_items.sort(key=lambda x: x['recommended_qty'], reverse=reverse)
    elif sort_by == 'product_name':
        filtered_items.sort(key=lambda x: x['product_name'].upper(), reverse=reverse)
    elif sort_by == 'avg_daily_demand':
        filtered_items.sort(key=lambda x: x['avg_daily_demand'], reverse=reverse)
    else:
        # Default: estimated_value
        filtered_items.sort(key=lambda x: x['estimated_value'], reverse=reverse)

    # Pagination slice
    paged_items = filtered_items[offset : offset + limit]

    return OrdersByCategoryDivisionResponse(
        status="success",
        metadata={
            "total_matching_products": total_matching,
            "total_recommended_units": round(total_units, 2),
            "total_estimated_value": round(total_value, 2),
            "limit": limit,
            "offset": offset,
            "has_more": total_matching > (offset + len(paged_items)),
            "filters_applied": {
                "division": division,
                "category": category,
                "manufacturer": manufacturer,
                "status": status,
                "urgency": urgency,
                "min_recommended_qty": min_recommended_qty,
                "min_order_value": min_order_value,
                "search": search,
            }
        },
        summary_by_division=summary_by_division,
        summary_by_category=summary_by_category,
        items=[OrderItemResponse(**item) for item in paged_items],
    )


@app.get(
    '/procurement/divisions',
    tags=['agent-procurement'],
    summary="Agent API: List all marketing divisions with ordering metrics"
)
def list_procurement_divisions(
    category: Optional[str] = Query(None, description="Optional category filter"),
    status: Optional[str] = Query('PENDING', description="Proposal status filter (default: 'PENDING')"),
    db: Session = Depends(get_db)
):
    """
    Returns an aggregated list of all pharmaceutical marketing divisions / companies
    having active order proposals, sorted by total purchase value.
    """
    status = _resolve_param(status, 'PENDING')
    category = _resolve_param(category, None)

    code_to_cat = _get_product_category_map(db)

    stmt = select(ProcurementProposal)
    if status and status.upper() != 'ALL':
        stmt = stmt.where(ProcurementProposal.status == status.upper())
    proposals = db.scalars(stmt).all()

    div_data = defaultdict(lambda: {'count': 0, 'units': 0.0, 'value': 0.0, 'products': []})

    for p in proposals:
        if is_ignored_item_name(p.product_name):
            continue
        p_cat = code_to_cat.get(p.product_code, 'General / Active Shelf-Life')
        if category and category.strip().lower() != 'all':
            if category.strip().lower() not in p_cat.lower():
                continue

        p_div = p.company or 'General'
        div_data[p_div]['count'] += 1
        div_data[p_div]['units'] += p.recommended_qty
        div_data[p_div]['value'] += p.estimated_value
        div_data[p_div]['products'].append({
            'product_code': p.product_code,
            'product_name': p.product_name,
            'recommended_qty': p.recommended_qty,
            'estimated_value': p.estimated_value
        })

    result_divisions = []
    for div_name, data in sorted(div_data.items(), key=lambda x: x[1]['value'], reverse=True):
        top_prods = sorted(data['products'], key=lambda x: x['estimated_value'], reverse=True)[:3]
        result_divisions.append({
            'division': div_name,
            'pending_order_count': data['count'],
            'total_recommended_qty': round(data['units'], 2),
            'total_estimated_value': round(data['value'], 2),
            'top_products': top_prods
        })

    return {
        'status': 'success',
        'total_divisions': len(result_divisions),
        'divisions': result_divisions
    }


@app.get(
    '/procurement/categories',
    tags=['agent-procurement'],
    summary="Agent API: List all product categories with ordering metrics"
)
def list_procurement_categories(
    division: Optional[str] = Query(None, description="Optional division filter"),
    status: Optional[str] = Query('PENDING', description="Proposal status filter (default: 'PENDING')"),
    db: Session = Depends(get_db)
):
    """
    Returns an aggregated list of all product categories having active order proposals,
    sorted by total purchase value.
    """
    status = _resolve_param(status, 'PENDING')
    division = _resolve_param(division, None)

    code_to_cat = _get_product_category_map(db)

    stmt = select(ProcurementProposal)
    if status and status.upper() != 'ALL':
        stmt = stmt.where(ProcurementProposal.status == status.upper())
    proposals = db.scalars(stmt).all()

    cat_data = defaultdict(lambda: {'count': 0, 'units': 0.0, 'value': 0.0})

    for p in proposals:
        if is_ignored_item_name(p.product_name):
            continue
        p_div = p.company or 'General'
        if division and division.strip().lower() != 'all':
            if division.strip().lower() not in p_div.lower():
                continue

        p_cat = code_to_cat.get(p.product_code, 'General / Active Shelf-Life')
        cat_data[p_cat]['count'] += 1
        cat_data[p_cat]['units'] += p.recommended_qty
        cat_data[p_cat]['value'] += p.estimated_value

    result_categories = [
        {
            'category': cat_name,
            'pending_order_count': data['count'],
            'total_recommended_qty': round(data['units'], 2),
            'total_estimated_value': round(data['value'], 2),
        }
        for cat_name, data in sorted(cat_data.items(), key=lambda x: x[1]['value'], reverse=True)
    ]

    return {
        'status': 'success',
        'total_categories': len(result_categories),
        'categories': result_categories
    }


@app.get(
    '/procurement/divisions-categories-summary',
    tags=['agent-procurement'],
    summary="Agent API: Hierarchical Division -> Category breakdown"
)
def get_divisions_categories_summary(
    status: Optional[str] = Query('PENDING', description="Proposal status filter (default: 'PENDING')"),
    db: Session = Depends(get_db)
):
    """
    Returns a complete hierarchical tree (Division -> Categories) for fast macro-level
    decision making by autonomous agents.
    """
    status = _resolve_param(status, 'PENDING')

    code_to_cat = _get_product_category_map(db)

    stmt = select(ProcurementProposal)
    if status and status.upper() != 'ALL':
        stmt = stmt.where(ProcurementProposal.status == status.upper())
    proposals = db.scalars(stmt).all()

    hierarchy = defaultdict(lambda: {
        'total_items': 0,
        'total_units': 0.0,
        'total_value': 0.0,
        'categories': defaultdict(lambda: {'item_count': 0, 'total_units': 0.0, 'total_value': 0.0})
    })

    total_prods = 0
    grand_value = 0.0

    for p in proposals:
        if is_ignored_item_name(p.product_name):
            continue
        p_div = p.company or 'General'
        p_cat = code_to_cat.get(p.product_code, 'General / Active Shelf-Life')

        hierarchy[p_div]['total_items'] += 1
        hierarchy[p_div]['total_units'] += p.recommended_qty
        hierarchy[p_div]['total_value'] += p.estimated_value

        hierarchy[p_div]['categories'][p_cat]['item_count'] += 1
        hierarchy[p_div]['categories'][p_cat]['total_units'] += p.recommended_qty
        hierarchy[p_div]['categories'][p_cat]['total_value'] += p.estimated_value

        total_prods += 1
        grand_value += p.estimated_value

    output_divisions = []
    for div_name, div_info in sorted(hierarchy.items(), key=lambda x: x[1]['total_value'], reverse=True):
        cats_list = [
            {
                'category': c_name,
                'item_count': c_data['item_count'],
                'total_units': round(c_data['total_units'], 2),
                'total_value': round(c_data['total_value'], 2)
            }
            for c_name, c_data in sorted(div_info['categories'].items(), key=lambda x: x[1]['total_value'], reverse=True)
        ]
        output_divisions.append({
            'division': div_name,
            'total_items': div_info['total_items'],
            'total_units': round(div_info['total_units'], 2),
            'total_value': round(div_info['total_value'], 2),
            'categories': cats_list
        })

    return {
        'status': 'success',
        'total_divisions': len(output_divisions),
        'total_products_to_order': total_prods,
        'total_estimated_value': round(grand_value, 2),
        'divisions': output_divisions
    }


@app.get('/proposals/{proposal_id}', tags=['proposals'])
def get_proposal(proposal_id: int, db: Session = Depends(get_db)):
    """
    Fetch a single procurement proposal by its unique ID.

    Args:
        proposal_id (int): Primary key ID of the proposal.
        db (Session): Database session.

    Returns:
        dict: Serialized proposal details.

    Raises:
        HTTPException: 404 if the proposal does not exist.
    """
    p = db.get(ProcurementProposal, proposal_id)
    if not p:
        raise HTTPException(status_code=404, detail='Proposal not found.')
    return _proposal_dict(p)


def _proposal_dict(p: ProcurementProposal) -> dict:
    """
    Convert a ProcurementProposal ORM entity into a serializable dictionary.

    Args:
        p (ProcurementProposal): Proposal entity instance.

    Returns:
        dict: Detailed proposal properties for UI consumption.
    """
    return {
        'id': p.id,
        'run_id': p.run_id,
        'product_code': p.product_code,
        'product_name': p.product_name,
        'company': p.company or 'General',
        'manufacturer': p.manufacturer or 'General',
        'batch_numbers': p.batch_numbers or 'DEFAULT',
        'supplier_id': p.supplier_id,
        'supplier_name': p.supplier_name,
        'recommended_qty': p.recommended_qty,
        'approved_qty': p.approved_qty,
        'unit_cost': p.unit_cost,
        'estimated_value': p.estimated_value,
        'avg_daily_demand': p.avg_daily_demand,
        'demand_source': p.demand_source,
        'lead_time_days': p.lead_time_days,
        'review_days': getattr(p, 'review_days', 7) or 7,
        'safety_days': getattr(p, 'safety_days', 3) or 3,
        'target_coverage_days': (p.lead_time_days or 45) + (getattr(p, 'review_days', 7) or 7) + (getattr(p, 'safety_days', 3) or 3),
        'stock_on_hand': p.stock_on_hand,
        'stock_on_order': p.stock_on_order,
        'near_expiry_qty': p.near_expiry_qty,
        'expiry_risk_score': p.expiry_risk_score,
        'expiry_action': p.expiry_action,
        'rationale': p.rationale,
        'status': p.status,
        'human_reason': p.human_reason,
        'approved_at': p.approved_at.isoformat() if p.approved_at else None,
        'executed_at': p.executed_at.isoformat() if p.executed_at else None,
        'execution_reference': p.execution_reference,
        'created_at': p.created_at.isoformat() if p.created_at else None,
    }


# ---------------------------------------------------------------------------
# Human approval / rejection
# ---------------------------------------------------------------------------
class DecisionRequest(BaseModel):
    action: str                        # 'approve' | 'reject'
    approved_qty: Optional[float] = None
    supplier_id: Optional[str] = None
    reason: Optional[str] = None
    actor: str = 'human-ui'


@app.post('/proposals/{proposal_id}/decide', tags=['proposals'])
def decide_proposal(proposal_id: int, req: DecisionRequest, db: Session = Depends(get_db)):
    """Approve or reject a PENDING proposal."""
    svc = ProposalService(db)
    try:
        result = svc.decide(
            proposal_id=proposal_id,
            action=req.action,
            approved_qty=req.approved_qty,
            supplier_id=req.supplier_id,
            reason=req.reason,
            actor=req.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


class BatchDecisionRequest(BaseModel):
    action: str = 'approve'                        # 'approve' | 'reject' | 'unapprove' | 'revert'
    proposal_ids: Optional[list[int]] = None      # None means all relevant proposals
    reason: Optional[str] = None
    actor: str = 'human-ui'


@app.post('/proposals/batch-decide', tags=['proposals'])
def batch_decide_proposals(req: BatchDecisionRequest, db: Session = Depends(get_db)):
    """Batch approve, reject, or revert proposals."""
    svc = ProposalService(db)
    if req.action in ('unapprove', 'revert'):
        stmt = select(ProcurementProposal).where(
            ProcurementProposal.status.in_(['APPROVED_PENDING_EXECUTION', 'EXECUTED'])
        )
    else:
        stmt = select(ProcurementProposal).where(ProcurementProposal.status == 'PENDING')

    if req.proposal_ids:
        stmt = stmt.where(ProcurementProposal.id.in_(req.proposal_ids))
    proposals = db.scalars(stmt).all()

    succeeded = 0
    errors = []
    for p in proposals:
        try:
            svc.decide(
                proposal_id=p.id,
                action=req.action,
                reason=req.reason,
                actor=req.actor,
            )
            succeeded += 1
        except Exception as exc:
            errors.append(f"Proposal #{p.id} ({p.product_name}): {exc}")

    return {
        "status": "success",
        "action": req.action,
        "processed": succeeded,
        "total_attempted": len(proposals),
        "errors": errors[:10],
    }


@app.post('/proposals/{proposal_id}/unapprove', tags=['proposals'])
def unapprove_proposal(
    proposal_id: int,
    req: Optional[DecisionRequest] = None,
    db: Session = Depends(get_db)
):
    """Reverts an approved proposal back to PENDING review."""
    svc = ProposalService(db)
    reason = req.reason if req and req.reason else "Removed from approved list by reviewer."
    actor = req.actor if req and req.actor else "human-ui"
    try:
        result = svc.decide(
            proposal_id=proposal_id,
            action="unapprove",
            reason=reason,
            actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
@app.get('/audit', tags=['audit'])
def list_audit(limit: int = 100, db: Session = Depends(get_db)):
    """
    Retrieve recent immutable audit events for tracking decisions, runs, and executions.

    Args:
        limit (int): Maximum number of log events to return (default 100).
        db (Session): Database session.

    Returns:
        list[dict]: List of audit entries sorted newest-first.
    """
    events = db.scalars(
        select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit)
    ).all()
    return [
        {
            'id': e.id,
            'event_type': e.event_type,
            'actor': e.actor,
            'entity_type': e.entity_type,
            'entity_id': e.entity_id,
            'details': e.details_json,
            'created_at': e.created_at.isoformat() if e.created_at else None,
        }
        for e in events
    ]


# ---------------------------------------------------------------------------
# Inventory Positions (Aggregated Product View)
# ---------------------------------------------------------------------------
@app.get('/inventory', tags=['inventory'])
def list_inventory(
    company: Optional[str] = None,
    manufacturer: Optional[str] = None,
    include_promo: bool = False,
    include_zero_stock: bool = False,
    exclude_healthy: bool = False,
    db: Session = Depends(get_db)
):
    """
    Fetch inventory grouped by Product Name and Expiry Sub-Category (FEFO Shelf-Life Status).
    - Excludes batches with Zero stock (qty_on_hand <= 0).
    - Groups all batches having the same product name and same expiry sub-category together.
    - The batch_no column contains ONLY the batches that belong to that specific category.
    - If exclude_healthy=True, filters out '✅ Shelf-Life Healthy' items.
    """
    now_dt = datetime.utcnow()
    horizon_days = int(settings.expiry_risk_horizon_days if settings else 180)
    horizon_dt = now_dt + timedelta(days=horizon_days)

    def get_fefo_category(exp_dt):
        if not exp_dt:
            return 'Unknown / General'
        if exp_dt <= now_dt:
            return '⛔ Expired'
        elif exp_dt <= horizon_dt:
            return f'⚠️ Near-Expiry (≤ {horizon_days}d)'
        else:
            return '✅ Shelf-Life Healthy'

    p_stmt = select(Product)
    if not include_promo:
        p_stmt = p_stmt.where(Product.is_promo_material == False)
    if company and company.lower() != 'all companies':
        p_stmt = p_stmt.where(Product.company == company)
    if manufacturer and manufacturer.lower() != 'all manufacturers':
        p_stmt = p_stmt.where(Product.manufacturer == manufacturer)
    products = db.scalars(p_stmt).all()

    # Product map by code
    prod_map = {}
    for p in products:
        if p.product_code not in prod_map:
            prod_map[p.product_code] = p

    all_batches = db.scalars(select(InventoryBatch)).all()

    # Filter batches: exclude zero-stock batches unless explicitly requested
    if not include_zero_stock:
        target_batches = [b for b in all_batches if b.qty_on_hand > 0 and b.product_code in prod_map]
    else:
        target_batches = [b for b in all_batches if b.product_code in prod_map]

    healthy_no_reorder_codes = set()
    healthy_no_reorder_names = set()
    if exclude_healthy:
        try:
            agent = ProcurementAgent(db)
            no_reorder_items = agent.get_no_reorder_products()
            healthy_no_reorder_codes = {
                p['product_code'] for p in no_reorder_items
                if p.get('expiry_action') != 'PAUSE_PROCUREMENT'
                and p.get('near_expiry_qty', 0) <= 0
                and p.get('expired_qty', 0) <= 0
            }
            healthy_no_reorder_names = {
                str(p.get('product_name', '')).strip().upper() for p in no_reorder_items
                if p.get('expiry_action') != 'PAUSE_PROCUREMENT'
                and p.get('near_expiry_qty', 0) <= 0
                and p.get('expired_qty', 0) <= 0
            }
        except Exception as e:
            logger.warning(f"Could not calculate no-reorder exclusions for inventory: {e}")

    # Group by (product_name, fefo_category)
    groups = defaultdict(list)
    for b in target_batches:
        p = prod_map.get(b.product_code)
        pname = p.product_name if p else b.product_code
        cat = get_fefo_category(b.expiry_date)
        if exclude_healthy:
            if cat.startswith('✅') or cat == 'Unknown / General':
                continue
            if b.product_code in healthy_no_reorder_codes or pname.strip().upper() in healthy_no_reorder_names:
                continue
            if is_ignored_item_name(pname):
                continue
        groups[(pname, cat)].append((p, b))

    results = []
    for (pname, cat), items in sorted(groups.items(), key=lambda x: str(x[0][0]).strip().upper()):
        p0, b0 = items[0]
        total_on_hand = sum(b.qty_on_hand for _, b in items)
        total_on_order = sum(b.qty_on_order for _, b in items)

        batch_nos = [b.batch_no for _, b in items if b.batch_no and b.batch_no != 'DEFAULT']
        batch_str = ", ".join(dict.fromkeys(batch_nos)) if batch_nos else 'DEFAULT'

        exp_dates = [b.expiry_date for _, b in items if b.expiry_date]
        min_exp = min(exp_dates) if exp_dates else None

        costs = [b.unit_cost for _, b in items if b.unit_cost and b.unit_cost > 0]
        unit_cost = costs[-1] if costs else ((p0.unit_cost if p0 else 0.0) or 0.0)
        total_val = sum((b.qty_on_hand * (b.unit_cost or unit_cost)) for _, b in items)

        comp = b0.company or (p0.company if p0 else 'General')
        mfr = b0.manufacturer or (p0.manufacturer if p0 else 'General')

        results.append({
            'product_code': p0.product_code if p0 else b0.product_code,
            'product_name': pname,
            'category': cat,
            'company': comp or 'General',
            'manufacturer': mfr or 'General',
            'batch_no': batch_str,
            'qty_on_hand': total_on_hand,
            'qty_on_order': total_on_order,
            'expiry_date': min_exp.strftime('%Y-%m-%d') if min_exp else None,
            'fefo_status': cat,
            'unit_cost': unit_cost,
            'inventory_value': round(total_val, 2),
        })

    return results


@app.get('/promo-material', tags=['inventory'])
def list_promo_material(
    company: Optional[str] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """
    Fetch promo material items (products where manufacturer and supplier were blank in stock Excel).
    Aggregated at the Product level: 1 row per product, sum of stock, comma-separated batches.
    """
    p_stmt = select(Product).where(Product.is_promo_material == True)
    if company and company.lower() != 'all companies':
        p_stmt = p_stmt.where(Product.company == company)
    p_stmt = p_stmt.order_by(Product.product_name.asc(), Product.batch_no.asc())
    promo_products = db.scalars(p_stmt).all()

    results = []
    for p in promo_products:
        qty = p.current_stock or 0.0
        unit_cost = p.unit_cost or 0.0
        exp_str = p.expiry_date.strftime('%Y-%m-%d') if p.expiry_date else 'N/A'

        item = {
            'product_code': p.product_code,
            'product_name': p.product_name,
            'category': 'Promo Material',
            'company': p.company or 'General',
            'manufacturer': p.manufacturer or 'None (Promo)',
            'supplier_name': p.supplier_name or 'None (Promo)',
            'batch_no': p.batch_no or 'DEFAULT',
            'qty_on_hand': qty,
            'unit_cost': unit_cost,
            'total_value': round(qty * unit_cost, 2),
            'expiry_date': exp_str,
            'unit': p.unit or 'pcs',
        }
        if search:
            s_low = search.strip().lower()
            if s_low not in item['product_name'].lower() and s_low not in item['product_code'].lower():
                continue
        results.append(item)

    return results


# ---------------------------------------------------------------------------
# MARG Excel Ingestion & Template
# ---------------------------------------------------------------------------
@app.get('/ingestion/sample-template', tags=['ingestion'])
def get_sample_marg_template():
    """Generates and downloads a realistic MARG Excel template with Stock, Sales, and Suppliers."""
    excel_bytes = create_sample_marg_excel()
    return Response(
        content=excel_bytes,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename="marg_sample_data.xlsx"'}
    )


@app.post('/ingestion/upload-marg-excel', tags=['ingestion'])
def upload_marg_excel(
    file: UploadFile = File(...),
    run_agent: bool = Query(True, description='Automatically run procurement agent after ingestion'),
    lead_time_days: Optional[int] = Query(None, description='Configurable lead time override in days'),
    review_days: Optional[int] = Query(None, description='Configurable review cycle override in days'),
    safety_days: Optional[int] = Query(None, description='Configurable safety buffer override in days'),
    manufacturer: Optional[str] = Query(None, description='Optional manufacturer scope override'),
    clear_existing: bool = Query(True, description='Clear all previous database records before ingesting fresh data'),
    db: Session = Depends(get_db)
):
    """
    Upload a MARG Excel file (.xlsx or .xls) or CSV.
    Ingests Products, Inventory Batches, Expiries, Suppliers, and Sales History.
    If clear_existing is True (default), completely purges stale historical data.
    If run_agent is True, immediately computes procurement recommendations!
    Executed in threadpool so FastAPI event loop and /health remain 100% responsive.
    """
    filename_lower = file.filename.lower()
    if not (filename_lower.endswith('.xlsx') or filename_lower.endswith('.xls') or filename_lower.endswith('.csv')):
        raise HTTPException(
            status_code=400,
            detail='Invalid file format. Please upload a MARG export file (.xlsx, .xls, or .csv).'
        )

    content = file.file.read()
    ingestion_service = IngestionService(db)

    logger.info(
        "Received file upload: %s (%s bytes), clear_existing=%s",
        file.filename,
        len(content) if 'content' in locals() else 0,
        clear_existing
    )
    try:
        stats = ingestion_service.ingest_excel(content, filename=file.filename, clear_existing=clear_existing)
        logger.info("Ingestion completed for %s: %s", file.filename, stats)
    except Exception as exc:
        logger.error("Failed to parse MARG Excel %s: %s", file.filename, exc, exc_info=True)
        raise HTTPException(status_code=400, detail=f'Failed to parse MARG Excel: {exc}')

    response_data = {
        'status': 'success',
        'filename': file.filename,
        'stats': stats,
        'agent_run': None,
        'proposals': [],
    }

    if run_agent:
        agent = ProcurementAgent(db)
        run_id, proposals = agent.run(
            lead_time_override=lead_time_days,
            review_days_override=review_days,
            safety_days_override=safety_days,
            manufacturer_override=manufacturer,
        )
        response_data['agent_run'] = {
            'run_id': run_id,
            'proposals_count': len(proposals),
        }
        response_data['proposals'] = [_proposal_dict(p) for p in proposals]

    return response_data


@app.post('/system/reset-db', tags=['system'])
def reset_database(db: Session = Depends(get_db)):
    """
    Wipes all operational data from the database:
    Products, Suppliers, Inventory Batches, Sales History, Procurement Runs, Proposals, Feedback.
    """
    ingestion_service = IngestionService(db)
    purged_stats = ingestion_service.purge_all_data()
    return {
        'status': 'success',
        'message': 'Database completely purged. Ready for fresh import.',
        'purged': purged_stats,
    }


# ---------------------------------------------------------------------------
# Live Rolling Logs
# ---------------------------------------------------------------------------
@app.get('/system/logs', tags=['system'])
def get_system_logs(lines: int = 100):
    """Returns recent log lines from the running service."""
    log_candidates = [
        "/Users/debz/.gemini/antigravity-ide/brain/3aff40a2-008c-4545-841e-136020632ad6/.system_generated/tasks/task-72.log"
    ]
    for p in log_candidates:
        fpath = Path(p)
        if fpath.exists():
            with fpath.open('r', encoding='utf-8', errors='replace') as f:
                all_lines = f.readlines()
                return {
                    "source": str(fpath),
                    "total_lines": len(all_lines),
                    "lines": all_lines[-lines:],
                }
    return {"lines": [], "message": "Log file not found."}


# ---------------------------------------------------------------------------
# Sales Intelligence & Customer Recommendations
# ---------------------------------------------------------------------------
@app.get('/sales/customer-groups', tags=['sales'])
def list_sales_customer_groups(db: Session = Depends(get_db)):
    """Retrieves all distinct customer groups/categories from MARG customer master."""
    svc = SalesIntelligenceService(db)
    return svc.list_customer_groups()


@app.get('/sales/overview-metrics', tags=['sales'])
def get_sales_overview_metrics(group: Optional[str] = None, db: Session = Depends(get_db)):
    """Retrieves executive sales KPIs and customer sales revenue categorization slabs (bins), optionally filtered by category/group."""
    svc = SalesIntelligenceService(db)
    return svc.get_sales_dashboard_overview(group=group)


@app.get('/sales/customers', tags=['sales'])
def list_sales_customers(
    search: Optional[str] = None,
    group: Optional[str] = None,
    limit: int = 5000,
    db: Session = Depends(get_db)
):
    """Retrieves all customers with total sales and current dues, optionally filtered by group."""
    svc = SalesIntelligenceService(db)
    return svc.list_customers(search=search, group=group, limit=limit)


@app.get('/sales/customers/{customer_code}/summary', tags=['sales'])
def get_customer_summary(
    customer_code: str,
    db: Session = Depends(get_db)
):
    """Retrieves detailed financial summary, sales 30d/90d, ageing, and history for a customer."""
    svc = SalesIntelligenceService(db)
    return svc.get_customer_summary(customer_code)


@app.get('/sales/customers/{customer_code}/products', tags=['sales'])
def get_customer_transacted_products(
    customer_code: str,
    db: Session = Depends(get_db)
):
    """Retrieves the list of products transacted by a customer from the Product-Wise Customer Ledger."""
    svc = SalesIntelligenceService(db)
    summary = svc.get_customer_summary(customer_code)
    return summary.get('transacted_products', [])


@app.get('/sales/customers/{customer_code}/recommendations', tags=['sales'])
def get_customer_recommendations(
    customer_code: str,
    db: Session = Depends(get_db)
):
    """Retrieves near-expiry opportunities and reorder candidates for a customer."""
    svc = SalesIntelligenceService(db)
    return svc.get_customer_recommendations(customer_code)


@app.post('/sales/upload-marg', tags=['sales'])
def upload_sales_marg_excel(
    file: UploadFile = File(...),
    clear_existing: bool = Query(False, description='Purge previous customer data'),
    db: Session = Depends(get_db)
):
    """Uploads MARG Sales Register, Customer Master, or Bill-wise Outstanding Excel/CSV file."""
    filename_lower = file.filename.lower()
    if not (filename_lower.endswith('.xlsx') or filename_lower.endswith('.xls') or filename_lower.endswith('.csv')):
        raise HTTPException(
            status_code=400,
            detail='Invalid file format. Please upload a MARG export file (.xlsx, .xls, or .csv).'
        )

    content = file.file.read()
    ingestion_service = IngestionService(db)
    try:
        stats = ingestion_service.ingest_excel(content, filename=file.filename, clear_existing=clear_existing)
    except Exception as exc:
        logger.error("Failed to parse MARG Sales Excel %s: %s", file.filename, exc, exc_info=True)
        raise HTTPException(status_code=400, detail=f'Failed to parse MARG Excel: {exc}')

    return {
        'status': 'success',
        'filename': file.filename,
        'stats': stats,
    }


@app.post('/sales/purge-customers', tags=['sales'])
def purge_sales_customers(db: Session = Depends(get_db)):
    """Purges all customer master records and outstanding receivables from the database."""
    ingestion_service = IngestionService(db)
    purged_stats = ingestion_service.purge_customer_data()
    return {
        'status': 'success',
        'message': 'Customer database purged successfully. Ready for fresh import.',
        'purged': purged_stats,
    }


@app.post('/sales/seed-demo', tags=['sales'])
def seed_demo_sales_data(db: Session = Depends(get_db)):
    """Seeds realistic demo customers, receivables, and sales history for instant evaluation."""
    stats = SalesIntelligenceService.seed_demo_data(db)
    return {
        'status': 'success',
        'message': 'Seeded demo customers, receivables, and sales transactions successfully.',
        'stats': stats,
    }


# ---------------------------------------------------------------------------
# Download Orders Agent — Web Automation via Playwright
# ---------------------------------------------------------------------------
@app.post('/procurement/download-initiated-orders', tags=['procurement'])
async def download_initiated_orders(
    output_dir: str | None = None,
    headless: bool | None = None,
):
    """
    Triggers the Playwright-based web automation agent to:
      1. Open http://www.bengalremedies.com:9000
      2. Login with configured credentials
      3. Navigate: Procurement → Order
      4. Filter by Status = Initiated
      5. Scrape ALL paginated table rows
      6. Save as Excel (.xlsx) and return metadata + base64 file data for direct web download
    """
    import base64
    from pathlib import Path
    try:
        from backend.app.agents.download_orders_agent import (
            run_download_agent,
            get_default_output_dir,
            is_cloud_environment,
        )
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Agent import error: {e}")

    effective_output_dir = output_dir or str(get_default_output_dir())
    effective_headless = headless if headless is not None else is_cloud_environment()

    try:
        result = await run_download_agent(
            output_dir=effective_output_dir,
            headless=effective_headless,
            status_filter="Initiated",
        )
        if result["success"]:
            file_bytes = result.get("file_bytes")
            if not file_bytes and result.get("file_path"):
                with open(result["file_path"], "rb") as f:
                    file_bytes = f.read()

            b64_str = base64.b64encode(file_bytes).decode("utf-8") if file_bytes else ""
            filename = result.get("filename") or (Path(result["file_path"]).name if result.get("file_path") else "initiated_orders.xlsx")

            return {
                "status": "success",
                "file_path": result["file_path"],
                "filename": filename,
                "file_b64": b64_str,
                "rows_downloaded": result["rows_downloaded"],
                "message": result["message"],
                "log": result["log"],
            }
        else:
            raise HTTPException(
                status_code=500,
                detail={
                    "message": result["message"],
                    "log": result["log"],
                }
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post('/procurement/download-initiated-orders-file', tags=['procurement'])
async def download_initiated_orders_file(
    headless: bool | None = None,
):
    """
    Runs the download orders agent and directly streams the resulting Excel (.xlsx) file
    as a downloadable browser attachment.
    """
    from pathlib import Path
    try:
        from backend.app.agents.download_orders_agent import (
            run_download_agent,
            get_default_output_dir,
            is_cloud_environment,
        )
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Agent import error: {e}")

    effective_headless = headless if headless is not None else is_cloud_environment()
    try:
        result = await run_download_agent(
            output_dir=get_default_output_dir(),
            headless=effective_headless,
            status_filter="Initiated",
        )
        if not result["success"]:
            raise HTTPException(status_code=500, detail=result.get("message", "Scrape failed"))

        file_bytes = result.get("file_bytes")
        if not file_bytes and result.get("file_path"):
            with open(result["file_path"], "rb") as f:
                file_bytes = f.read()

        filename = result.get("filename") or (Path(result["file_path"]).name if result.get("file_path") else "initiated_orders.xlsx")
        return Response(
            content=file_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post('/procurement/upload-ordered-items', tags=['procurement'])
async def upload_ordered_items(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Ingests an Excel or CSV file containing already-ordered items (Pipeline / Stock on Order).

    Guardrail:
    - If the catalog products table is empty, returns HTTP 400:
      "Please upload the stock and sales report first."
    """
    filename = file.filename or "ordered_items.xlsx"
    ext = Path(filename).suffix.lower()
    if ext not in ('.xlsx', '.xls', '.csv'):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file format '{ext}'. Please upload an Excel (.xlsx, .xls) or CSV file."
        )

    content = await file.read()
    ingestion_service = IngestionService(db)

    try:
        stats = ingestion_service.ingest_ordered_items_excel(content, filename)
        return {
            "status": "success",
            "message": f"Successfully ingested {stats['total_rows_processed']} ordered items. Updated pipeline stock for {stats['unique_products_updated']} products ({stats['total_units_on_order']} units on order).",
            "stats": stats,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to ingest ordered items: {e}")





