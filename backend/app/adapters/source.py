"""
MARG API Source Adapter

Provides an HTTP-based data ingestion boundary for deployments where MARG ERP exposes
direct REST API read endpoints instead of manual Excel exports.
"""

from __future__ import annotations

import json
from typing import Any
import requests

from backend.app.core.config import settings


class MargApiSourceError(RuntimeError):
    """Exception raised when querying or parsing responses from the MARG read API fails."""
    pass


class MargApiSourceAdapter:
    """
    HTTP REST API ingestion adapter for reading operational data directly from MARG ERP.

    Handles dataset dispatch (products, inventory_batches, suppliers, sales_history),
    authentication headers (API key, company code), and response payload normalization.
    """

    def __init__(self, endpoints: dict[str, str] | None = None) -> None:
        """
        Initialize the MARG API adapter.

        Args:
            endpoints (dict[str, str] | None): Optional map of dataset names to HTTP URLs.
                                              If omitted, loads from application settings.
        """
        self.endpoints: dict[str, str] = endpoints or self._load_endpoints()

    @staticmethod
    def _load_endpoints() -> dict[str, str]:
        """
        Parse and validate configured MARG data read endpoints from application settings.

        Returns:
            dict[str, str]: Dictionary mapping dataset identifier (e.g. 'products') to endpoint URL.

        Raises:
            MargApiSourceError: If the configuration JSON is malformed or invalid.
        """
        raw = settings.marg_data_endpoints_json.strip()
        if not raw:
            return {}
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("MARG_DATA_ENDPOINTS_JSON must be a JSON object.")
            return {str(k): str(v) for k, v in value.items()}
        except Exception as exc:
            raise MargApiSourceError(f"Invalid MARG_DATA_ENDPOINTS_JSON configuration: {exc}") from exc

    def fetch_dataset(self, dataset: str) -> list[dict[str, Any]]:
        """
        Fetch and parse a raw operational dataset from the configured MARG API endpoint.

        Args:
            dataset (str): The dataset name to retrieve ('products', 'suppliers', 'inventory_batches', etc.).

        Returns:
            list[dict[str, Any]]: List of dictionary records representing the raw rows.

        Raises:
            MargApiSourceError: If the source is disabled, endpoint is unconfigured,
                                HTTP request fails, or response structure cannot be parsed.
        """
        if not settings.marg_source_enabled:
            raise MargApiSourceError("MARG API source is disabled in application settings.")

        endpoint = self.endpoints.get(dataset)
        if not endpoint:
            raise MargApiSourceError(f"No MARG read endpoint configured for dataset {dataset!r}.")

        headers: dict[str, str] = {"Accept": "application/json"}
        if settings.marg_api_key:
            headers["Authorization"] = f"Bearer {settings.marg_api_key}"
        if settings.marg_company_code:
            headers["X-Company-Code"] = settings.marg_company_code

        try:
            response = requests.get(endpoint, headers=headers, timeout=settings.marg_api_timeout_seconds)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise MargApiSourceError(f"MARG API read request failed for dataset {dataset}: {exc}") from exc

        # 1. Plain list of row dicts
        if isinstance(body, list):
            return body

        # 2. Wrapped payload in standard envelope keys
        if isinstance(body, dict):
            for key in ("data", "items", "records", "results"):
                if isinstance(body.get(key), list):
                    return body[key]

            # 3. MARG specific Details envelope
            details = body.get("Details") or body.get("details")
            if isinstance(details, dict):
                lower = {str(k).lower(): v for k, v in details.items()}
                # Supplier / Party array
                if dataset == "suppliers" and isinstance(lower.get("party"), list):
                    return lower["party"]

                # Product / Inventory rows
                rows = []
                for key in ("pro_n", "pron", "pro_u", "prou"):
                    if isinstance(lower.get(key), list):
                        rows.extend(lower[key])

                if dataset in {"products", "inventory_batches"} and rows:
                    dedup: dict[str, dict] = {}
                    for row in rows:
                        if isinstance(row, dict):
                            code = str(row.get("code") or row.get("Code") or "").strip()
                            dedup[code or str(len(dedup))] = row
                    return list(dedup.values())

        raise MargApiSourceError(f"Unsupported MARG API response shape received for dataset {dataset}.")

