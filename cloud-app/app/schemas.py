from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


Direction = Literal["ZOHO_TO_TALLY", "TALLY_TO_ZOHO"]
Priority = Literal["LOW", "NORMAL", "HIGH", "CRITICAL"]
ObjectType = Literal[
    "CONTACT",
    "ITEM",
    "INVOICE",
    "BILL",
    "PAYMENT",
    "RECEIPT",
    "JOURNAL",
    "CREDIT_NOTE",
    "DEBIT_NOTE",
    "LEDGER",
]


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    zoho_org_id: str | None = Field(default=None, max_length=64)


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    zoho_org_id: str | None
    created_at: datetime


class ConnectorEnrollmentOut(BaseModel):
    connector_id: str
    enrollment_token: str
    secret: str


class HeartbeatIn(BaseModel):
    connector_id: str
    secret: str


class SyncJobCreate(BaseModel):
    tenant_id: str
    direction: Direction
    object_type: ObjectType
    source_id: str = Field(min_length=1, max_length=128)
    priority: Priority = "NORMAL"
    dependency_keys: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class SyncJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    direction: str
    object_type: str
    source_id: str
    priority: str
    status: str
    attempt: int
    dependency_keys: list[str]
    payload: dict
    error_message: str | None
    correlation_id: str
    created_at: datetime
    updated_at: datetime


class PullJobsIn(BaseModel):
    connector_id: str
    secret: str
    limit: int = Field(default=10, ge=1, le=100)


class JobAckIn(BaseModel):
    connector_id: str
    secret: str


class JobFailIn(BaseModel):
    connector_id: str
    secret: str
    error_message: str = Field(min_length=1, max_length=2000)


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str | None
    category: str
    action: str
    payload: dict
    created_at: datetime


# ============ ZOHO OAUTH SCHEMAS ============

class ZohoOAuthURLOut(BaseModel):
    auth_url: str


class ZohoOAuthCallbackIn(BaseModel):
    code: str
    state: str | None = None


class ZohoOAuthTokenOut(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str
    scope: str


# ============ RECONCILIATION SCHEMAS ============

class ReconciliationRunCreate(BaseModel):
    tenant_id: str
    run_type: Literal["TRIAL_BALANCE", "OPEN_INVOICES", "PAYMENTS", "STOCK"] = "TRIAL_BALANCE"
    period_from: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    period_to: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class ReconciliationRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    run_type: str
    period_from: str
    period_to: str
    status: str
    tally_total_debit: float
    tally_total_credit: float
    zoho_total_debit: float
    zoho_total_credit: float
    mismatch_count: int
    mismatch_details: dict
    error_message: str | None
    started_at: datetime
    completed_at: datetime | None


class TrialBalanceInput(BaseModel):
    ledger_name: str
    debit: float
    credit: float


class TrialBalanceReconcileIn(BaseModel):
    tenant_id: str
    period_from: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    period_to: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    tally_trial_balance: list[TrialBalanceInput]


class OpenInvoicesReconcileIn(BaseModel):
    tenant_id: str
    period_from: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    period_to: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    tally_open_invoices: list[dict]


class PaymentsReconcileIn(BaseModel):
    tenant_id: str
    period_from: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    period_to: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    tally_payments: list[dict]


# ============ TALLY-ZOHO MAPPING SCHEMAS ============

class TallyZohoMappingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    tally_object_type: str
    tally_object_id: str
    tally_object_name: str | None
    zoho_object_type: str
    zoho_object_id: str
    zoho_object_name: str | None
    sync_direction: str
    last_synced_at: datetime | None
    sync_hash: str | None
    created_at: datetime
    updated_at: datetime
