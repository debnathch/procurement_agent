"""Pydantic-settings config loaded from .env"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'MARG Procurement Agent'
    database_url: str = 'sqlite:///./procurement_agent.db'

    # Execution
    execution_mode: str = 'dry_run'        # dry_run | csv | marg
    require_human_approval: bool = True

    # Guardrails
    max_proposal_qty_units: float = 5000
    max_proposal_value: float = 250000

    # Policy defaults
    default_review_days: int = 7
    default_safety_days: int = 3
    expiry_risk_horizon_days: int = 90
    default_lead_time_days: int = 45

    # MARG integration (disabled by default)
    marg_source_enabled: bool = False
    marg_enabled: bool = False
    marg_api_key: str = ''
    marg_company_code: str = ''
    marg_purchase_order_endpoint: str = ''
    marg_data_endpoints_json: str = '{}'
    marg_api_timeout_seconds: int = 30

    # Policy knobs
    allow_supplier_change: bool = True


settings = Settings()
