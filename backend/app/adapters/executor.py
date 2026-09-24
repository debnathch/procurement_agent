"""
Purchase Order Execution Adapters

This module handles how approved procurement proposals are converted into real or simulated
purchase orders. It implements the Strategy pattern with three executor modes:

1. DryRunExecutor  : Safely simulates order placement without writing external files or calling APIs.
2. CsvExecutor     : Writes the finalized purchase order out to a CSV file in an 'outbox' folder.
3. MargHttpExecutor: Posts the order JSON to a live MARG ERP API endpoint (when enabled).

The factory function `build_executor()` selects the appropriate executor based on the
`EXECUTION_MODE` setting in `.env`.
"""

import csv
import hashlib
import json
from pathlib import Path
import requests

from backend.app.adapters.base import ExecutionResult
from backend.app.core.config import settings


class DryRunExecutor:
    """
    Simulated executor for safe testing and local development.

    Generates a deterministic reference number based on the proposal's idempotency key
    without calling any external service or creating local files.
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
        Simulate creating a purchase order in dry-run mode.

        Args:
            supplier_id (str): Unique identifier of the approved vendor/party.
            company_code (str): MARG company/store code.
            items (list[dict]): Line items to order, each containing:
                                product_code, quantity, unit_cost.
            remark (str): Human approval notes or order remarks.
            idempotency_key (str): Unique hash to prevent duplicate order submissions.

        Returns:
            ExecutionResult: Contains success flag (True), generated DRYRUN reference ID,
                             and an informative status message.
        """
        # Create a deterministic 12-character uppercase reference from SHA-1 of the idempotency key
        ref_hash = hashlib.sha1(idempotency_key.encode()).hexdigest()[:12].upper()
        reference = f"DRYRUN-{ref_hash}"

        return ExecutionResult(
            success=True,
            reference=reference,
            message="Dry-run: Order approved successfully; no external purchase order was posted.",
        )


class CsvExecutor:
    """
    File-based executor that exports approved purchase orders to CSV files.

    Useful for manual import into MARG ERP or sharing with procurement clerks
    via email or file shares.
    """

    def __init__(self, out_dir: str = "outbox"):
        """
        Initialize the CSV executor and ensure output directory exists.

        Args:
            out_dir (str): Relative or absolute path to the directory where CSVs will be saved.
                           Defaults to 'outbox'.
        """
        self.out_dir = Path(out_dir)
        # Create the directory if it does not already exist
        self.out_dir.mkdir(parents=True, exist_ok=True)

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
        Write the purchase order line items to a CSV file.

        Args:
            supplier_id (str): Identifier of the target vendor.
            company_code (str): Company/store identifier.
            items (list[dict]): List of items with product_code, quantity, unit_cost.
            remark (str): Order remarks or reviewer justification.
            idempotency_key (str): Unique key used to generate a safe, collision-free filename.

        Returns:
            ExecutionResult: Contains success flag (True), path to written CSV file, and confirmation message.
        """
        # Sanitize idempotency key so it's safe for use in filenames on any operating system
        safe_key = "".join(c for c in idempotency_key if c.isalnum() or c in "-_")[:80]
        csv_path = self.out_dir / f"purchase_order_{safe_key}.csv"

        # Define CSV columns compatible with standard procurement spreadsheets
        fieldnames = [
            "company_code",
            "supplier_id",
            "product_code",
            "quantity",
            "unit_cost",
            "remark",
            "idempotency_key",
        ]

        # Write lines to CSV
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for item in items:
                writer.writerow({
                    "company_code": company_code,
                    "supplier_id": supplier_id,
                    "product_code": item.get("product_code"),
                    "quantity": item.get("quantity"),
                    "unit_cost": item.get("unit_cost"),
                    "remark": remark,
                    "idempotency_key": idempotency_key,
                })

        return ExecutionResult(
            success=True,
            reference=str(csv_path),
            message=f"Purchase order written to {csv_path}.",
        )


class MargHttpExecutor:
    """
    Direct HTTP API executor that posts purchase orders into MARG ERP.

    Requires MARG_ENABLED=true and MARG_PURCHASE_ORDER_ENDPOINT configured in .env.
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
        Send a REST POST request to the MARG purchase order API endpoint.

        Args:
            supplier_id (str): Supplier account code in MARG.
            company_code (str): Multi-company code in MARG.
            items (list[dict]): Items to order with product_code, quantity, unit_cost.
            remark (str): Human notes/reason to attach to the PO header.
            idempotency_key (str): Prevents double-creation if network retries occur.

        Returns:
            ExecutionResult: Success status, MARG order number/reference, and response message.
        """
        # Safety check: Prevent network calls if disabled by configuration
        if not settings.marg_enabled or not settings.marg_purchase_order_endpoint:
            return ExecutionResult(
                success=False,
                reference="",
                message="MARG execution is not configured. Enable MARG_ENABLED and provide MARG_PURCHASE_ORDER_ENDPOINT in .env.",
            )

        # Prepare JSON payload matching standard MARG ERP web-connector schema
        payload = {
            "company_code": company_code,
            "supplier_id": supplier_id,
            "items": items,
            "remark": remark,
            "idempotency_key": idempotency_key,
        }

        # Set up authentication headers
        headers = {"Content-Type": "application/json"}
        if settings.marg_api_key:
            headers["Authorization"] = f"Bearer {settings.marg_api_key}"

        try:
            # Send HTTP POST request with a 30-second timeout
            response = requests.post(
                settings.marg_purchase_order_endpoint,
                json=payload,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()

            body = response.json() if response.content else {}

            # Extract order number from various common MARG response fields
            order_ref = str(body.get("order_no") or body.get("reference") or "")

            return ExecutionResult(
                success=True,
                reference=order_ref,
                message=json.dumps(body)[:1500],
            )
        except Exception as exc:
            # Capture failure gracefully and return failure result with error details
            return ExecutionResult(
                success=False,
                reference="",
                message=f"MARG API execution failed: {exc}",
            )


def build_executor():
    """
    Factory function to instantiate the active executor based on EXECUTION_MODE in .env.

    Supported modes:
    - 'dry_run' : Default safe mode; returns mock references.
    - 'csv'     : Saves orders to local CSV files in outbox/.
    - 'marg'    : Direct HTTP integration with MARG ERP API.

    Returns:
        PurchaseOrderExecutor: An instance conforming to the executor interface.

    Raises:
        ValueError: If an unrecognized EXECUTION_MODE is supplied.
    """
    mode = settings.execution_mode.lower()

    if mode == "dry_run":
        return DryRunExecutor()
    if mode == "csv":
        return CsvExecutor()
    if mode == "marg":
        return MargHttpExecutor()

    raise ValueError(
        f"Unsupported EXECUTION_MODE={settings.execution_mode!r}. Valid options: 'dry_run', 'csv', 'marg'."
    )
