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
