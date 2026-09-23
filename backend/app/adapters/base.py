from dataclasses import dataclass
from typing import Protocol

@dataclass
class ExecutionResult:
    success: bool
    reference: str
    message: str

class PurchaseOrderExecutor(Protocol):
    def create_purchase_order(self, *, supplier_id: str, company_code: str, items: list[dict], remark: str, idempotency_key: str) -> ExecutionResult: ...
