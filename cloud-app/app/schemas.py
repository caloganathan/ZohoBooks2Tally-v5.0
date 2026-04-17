from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TenantCreate(BaseModel):
    name: str
    zoho_org_id: str | None = None


class TenantOut(BaseModel):
    id: str
    name: str
    zoho_org_id: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class ConnectorEnrollmentOut(BaseModel):
    connector_id: str
    enrollment_token: str
    secret: str


class HeartbeatIn(BaseModel):
    connector_id: str
    secret: str


class SyncJobCreate(BaseModel):
    tenant_id: str
    direction: str
    object_type: str
    source_id: str
    priority: str = "NORMAL"
    dependency_keys: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class SyncJobOut(BaseModel):
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

    class Config:
        from_attributes = True


class PullJobsIn(BaseModel):
    connector_id: str
    secret: str
    limit: int = 10


class JobAckIn(BaseModel):
    connector_id: str
    secret: str


class JobFailIn(BaseModel):
    connector_id: str
    secret: str
    error_message: str


class AuditOut(BaseModel):
    id: str
    tenant_id: str | None
    category: str
    action: str
    payload: dict
    created_at: datetime

    class Config:
        from_attributes = True
