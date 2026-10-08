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
    return {'status': 'ok', 'app': settings.app_name, 'mode': settings.execution_mode}


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
# Agent — run procurement
# ---------------------------------------------------------------------------
class RunRequest(BaseModel):
    product_codes: Optional[list[str]] = None
    lead_time_days: Optional[int] = None


@app.post('/runs', tags=['agent'], status_code=status.HTTP_201_CREATED)
def trigger_run(req: RunRequest = RunRequest(), db: Session = Depends(get_db)):
    """Trigger a procurement agent run. Returns run_id and proposal count."""
    agent = ProcurementAgent(db)
    run_id, proposals = agent.run(product_codes=req.product_codes, lead_time_override=req.lead_time_days)
    return {'run_id': run_id, 'proposals': len(proposals)}


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
    search: Optional[str] = None,
    company: Optional[str] = None,
    manufacturer: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """
    Returns active products with Net Need < 0, ordered alphabetically by character.
    Supports optional search, company, and manufacturer filtering.
    """
    agent = ProcurementAgent(db)
    items = agent.get_no_reorder_products(lead_time_override=lead_time_days)
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
        run_id, proposals = agent.run(lead_time_override=lead_time_days)
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
    zero_sales_dues_gt_zero: bool = False,
    limit: int = 5000,
    db: Session = Depends(get_db)
):
    """Retrieves all customers with total sales and current dues, optionally filtered by group or zero sales with dues > 0."""
    svc = SalesIntelligenceService(db)
    return svc.list_customers(
        search=search,
        group=group,
        zero_sales_dues_gt_zero=zero_sales_dues_gt_zero,
        limit=limit
    )


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



