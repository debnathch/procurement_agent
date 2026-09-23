import csv
import hashlib
import json
from pathlib import Path
import requests
from backend.app.adapters.base import ExecutionResult
from backend.app.core.config import settings

class DryRunExecutor:
    def create_purchase_order(self, *, supplier_id, company_code, items, remark, idempotency_key):
        ref = f'DRYRUN-{hashlib.sha1(idempotency_key.encode()).hexdigest()[:12].upper()}'
        return ExecutionResult(True, ref, 'Dry-run: no external purchase order was posted.')

class CsvExecutor:
    def __init__(self, out_dir='outbox'):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
    def create_purchase_order(self, *, supplier_id, company_code, items, remark, idempotency_key):
        safe_key = ''.join(c for c in idempotency_key if c.isalnum() or c in '-_')[:80]
        path = self.out_dir / f'purchase_order_{safe_key}.csv'
        with path.open('w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=['company_code','supplier_id','product_code','quantity','unit_cost','remark','idempotency_key'])
            w.writeheader()
            for item in items:
                w.writerow({'company_code': company_code, 'supplier_id': supplier_id, **item, 'remark': remark, 'idempotency_key': idempotency_key})
        return ExecutionResult(True, str(path), f'Purchase order written to {path}.')

class MargHttpExecutor:
    def create_purchase_order(self, *, supplier_id, company_code, items, remark, idempotency_key):
        if not settings.marg_enabled or not settings.marg_purchase_order_endpoint:
            return ExecutionResult(False, '', 'MARG execution is not configured.')
        payload={'company_code':company_code,'supplier_id':supplier_id,'items':items,'remark':remark,'idempotency_key':idempotency_key}
        headers={'Content-Type':'application/json'}
        if settings.marg_api_key: headers['Authorization']=f'Bearer {settings.marg_api_key}'
        try:
            r=requests.post(settings.marg_purchase_order_endpoint,json=payload,headers=headers,timeout=30)
            r.raise_for_status()
            body=r.json() if r.content else {}
            ref=str(body.get('order_no') or body.get('reference') or '')
            return ExecutionResult(True,ref,json.dumps(body)[:1500])
        except Exception as exc:
            return ExecutionResult(False,'',f'Execution failed: {exc}')

def build_executor():
    mode=settings.execution_mode.lower()
    if mode=='dry_run': return DryRunExecutor()
    if mode=='csv': return CsvExecutor()
    if mode=='marg': return MargHttpExecutor()
    raise ValueError(f'Unsupported EXECUTION_MODE={settings.execution_mode!r}')
