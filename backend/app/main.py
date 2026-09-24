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

from backend.app.core.config import settings
from backend.app.core.database import get_db, init_db
from backend.app.models.entities import (
    ProcurementProposal, ProcurementRun, Product, Supplier,
    InventoryBatch, SalesHistory, AuditEvent, FeedbackEvent,
)
from backend.app.agent.procurement_agent import ProcurementAgent
from backend.app.services.feedback import ProposalService
from backend.app.services.ingestion import IngestionService
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
    products = db.scalars(select(Product)).all()
    return [_product_dict(p) for p in products]


@app.get('/products/{product_code}', tags=['products'])
def get_product(product_code: str, db: Session = Depends(get_db)):
    p = db.get(Product, product_code)
    if not p:
        raise HTTPException(status_code=404, detail='Product not found.')
    return _product_dict(p)


def _product_dict(p: Product) -> dict:
    return {
        'product_code': p.product_code,
        'product_name': p.product_name,
        'category': p.category,
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
    suppliers = db.scalars(select(Supplier)).all()
    return [_supplier_dict(s) for s in suppliers]


def _supplier_dict(s: Supplier) -> dict:
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


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------
@app.get('/proposals', tags=['proposals'])
def list_proposals(status_filter: Optional[str] = None, db: Session = Depends(get_db)):
    stmt = select(ProcurementProposal).order_by(ProcurementProposal.created_at.desc())
    if status_filter:
        stmt = stmt.where(ProcurementProposal.status == status_filter.upper())
    proposals = db.scalars(stmt).all()
    return [_proposal_dict(p) for p in proposals]


@app.get('/proposals/{proposal_id}', tags=['proposals'])
def get_proposal(proposal_id: int, db: Session = Depends(get_db)):
    p = db.get(ProcurementProposal, proposal_id)
    if not p:
        raise HTTPException(status_code=404, detail='Proposal not found.')
    return _proposal_dict(p)


def _proposal_dict(p: ProcurementProposal) -> dict:
    return {
        'id': p.id,
        'run_id': p.run_id,
        'product_code': p.product_code,
        'product_name': p.product_name,
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


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
@app.get('/audit', tags=['audit'])
def list_audit(limit: int = 100, db: Session = Depends(get_db)):
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
# Inventory Batches
# ---------------------------------------------------------------------------
@app.get('/inventory', tags=['inventory'])
def list_inventory(db: Session = Depends(get_db)):
    query = (
        select(InventoryBatch, Product.product_name, Product.category)
        .outerjoin(Product, InventoryBatch.product_code == Product.product_code)
        .order_by(Product.product_name, InventoryBatch.batch_no)
    )
    rows = db.execute(query).all()
    return [
        {
            'id': b.id,
            'product_code': b.product_code,
            'product_name': product_name or b.product_code,
            'category': category or 'General',
            'batch_no': b.batch_no,
            'qty_on_hand': b.qty_on_hand,
            'qty_on_order': b.qty_on_order,
            'expiry_date': b.expiry_date.strftime('%Y-%m-%d') if b.expiry_date else None,
            'unit_cost': b.unit_cost,
        }
        for b, product_name, category in rows
    ]


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
async def upload_marg_excel(
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
    """
    filename_lower = file.filename.lower()
    if not (filename_lower.endswith('.xlsx') or filename_lower.endswith('.xls') or filename_lower.endswith('.csv')):
        raise HTTPException(
            status_code=400,
            detail='Invalid file format. Please upload a MARG export file (.xlsx, .xls, or .csv).'
        )

    content = await file.read()
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


