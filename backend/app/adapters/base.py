"""
Base Interfaces & Data Structures for Purchase Order Executors

Defines the contract that any order executor (Dry-Run, CSV file writer, MARG API client)
must implement.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ExecutionResult:
    """
    Standardized result object returned after attempting order execution.

    Attributes:
        success (bool): Whether the purchase order was successfully created or simulated.
        reference (str): External order number, file path, or mock reference ID.
        message (str): Human-readable confirmation or error message.
    """
    success: bool
    reference: str
    message: str


class PurchaseOrderExecutor(Protocol):
    """
    Protocol (interface) defining the required method for any order executor.
    """

    def create_purchase_order(
        self,
        *,
        supplier_id: str,
        company_code: str,
        items: list[dict],
        remark: str,
        idempotency_key: str,
    ) -> ExecutionResult:
        """
        Create or dispatch a purchase order.

        Args:
            supplier_id (str): Identifier of the target vendor.
            company_code (str): Organization or company code in MARG.
            items (list[dict]): Line items to buy (product_code, quantity, unit_cost).
            remark (str): Order remarks or reviewer notes.
            idempotency_key (str): Unique idempotency key to prevent double submissions.

        Returns:
            ExecutionResult: Result containing success flag, reference, and message.
        """
        ...
