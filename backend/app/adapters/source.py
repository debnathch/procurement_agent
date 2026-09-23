from __future__ import annotations
import json
from typing import Any
import requests
from backend.app.core.config import settings

class MargApiSourceError(RuntimeError): pass

class MargApiSourceAdapter:
    """Future MARG API ingestion boundary. Exact endpoints remain deployment-specific."""
    def __init__(self,endpoints:dict[str,str]|None=None): self.endpoints=endpoints or self._load_endpoints()
    @staticmethod
    def _load_endpoints():
        raw=settings.marg_data_endpoints_json.strip()
        if not raw: return {}
        try:
            value=json.loads(raw)
            if not isinstance(value,dict): raise ValueError('MARG_DATA_ENDPOINTS_JSON must be an object.')
            return {str(k):str(v) for k,v in value.items()}
        except Exception as exc: raise MargApiSourceError(f'Invalid MARG_DATA_ENDPOINTS_JSON: {exc}') from exc
    def fetch_dataset(self,dataset:str)->list[dict[str,Any]]:
        if not settings.marg_source_enabled:
            raise MargApiSourceError('MARG API source is disabled.')
        endpoint=self.endpoints.get(dataset)
        if not endpoint: raise MargApiSourceError(f'No MARG read endpoint configured for dataset {dataset!r}.')
        headers={'Accept':'application/json'}
        if settings.marg_api_key: headers['Authorization']=f'Bearer {settings.marg_api_key}'
        if settings.marg_company_code: headers['X-Company-Code']=settings.marg_company_code
        try:
            response=requests.get(endpoint,headers=headers,timeout=settings.marg_api_timeout_seconds)
            response.raise_for_status(); body=response.json()
        except Exception as exc: raise MargApiSourceError(f'MARG API read failed for {dataset}: {exc}') from exc
        if isinstance(body,list): return body
        if isinstance(body,dict):
            for key in ('data','items','records','results'):
                if isinstance(body.get(key),list): return body[key]
            details=body.get('Details') or body.get('details')
            if isinstance(details,dict):
                lower={str(k).lower():v for k,v in details.items()}
                if dataset=='suppliers' and isinstance(lower.get('party'),list): return lower['party']
                rows=[]
                for key in ('pro_n','pron','pro_u','prou'):
                    if isinstance(lower.get(key),list): rows.extend(lower[key])
                if dataset in {'products','inventory_batches'} and rows:
                    dedup={}
                    for row in rows:
                        if isinstance(row,dict):
                            code=str(row.get('code') or row.get('Code') or '').strip()
                            dedup[code or str(len(dedup))]=row
                    return list(dedup.values())
        raise MargApiSourceError('Unsupported MARG API response shape.')
