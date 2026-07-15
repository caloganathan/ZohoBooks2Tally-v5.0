from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def uuid_str() -> str:
    return str(uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    zoho_org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    connectors: Mapped[list["Connector"]] = relationship(back_populates="tenant")
    zoho_tokens: Mapped[list["ZohoToken"]] = relationship(back_populates="tenant")


class Connector(Base):
    __tablename__ = "connectors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    enrollment_token: Mapped[str] = mapped_column(String(72), nullable=False)
    # Encrypted at rest (Fernet ciphertext is longer than the plaintext secret).
    secret: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ENROLLED")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    tenant: Mapped[Tenant] = relationship(back_populates="connectors")


class ZohoToken(Base):
    __tablename__ = "zoho_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, unique=True)
    # Encrypted at rest via app.crypto (Fernet). Text to fit ciphertext length.
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[int] = mapped_column(nullable=False)
    scope: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    tenant: Mapped[Tenant] = relationship(back_populates="zoho_tokens")


class SyncJob(Base):
    __tablename__ = "sync_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    priority: Mapped[str] = mapped_column(String(16), default="NORMAL")
    status: Mapped[str] = mapped_column(String(24), default="QUEUED")
    attempt: Mapped[int] = mapped_column(default=0)
    dependency_keys: Mapped[list[str]] = mapped_column(JSON, default=list)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(36), default=uuid_str)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index("ix_sync_jobs_tenant_status", "tenant_id", "status"),
        Index("ix_sync_jobs_dedupe", "tenant_id", "direction", "object_type", "source_id"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class TallyZohoMapping(Base):
    """Cross-reference mapping between Tally and Zoho Books objects."""
    __tablename__ = "tally_zoho_mappings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    tally_object_type: Mapped[str] = mapped_column(String(64), nullable=False)  # LEDGER, STOCKITEM, VOUCHER, etc.
    tally_object_id: Mapped[str] = mapped_column(String(128), nullable=False)  # Tally GUID or ID
    tally_object_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    zoho_object_type: Mapped[str] = mapped_column(String(64), nullable=False)  # CONTACT, ITEM, INVOICE, etc.
    zoho_object_id: Mapped[str] = mapped_column(String(128), nullable=False)   # Zoho Books ID
    zoho_object_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sync_direction: Mapped[str] = mapped_column(String(32), default="TALLY_TO_ZOHO")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sync_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)  # For change detection
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "tally_object_type", "tally_object_id", name="uq_tally_mapping"),
        UniqueConstraint("tenant_id", "zoho_object_type", "zoho_object_id", name="uq_zoho_mapping"),
        Index("ix_mapping_tenant_tally", "tenant_id", "tally_object_type", "tally_object_id"),
        Index("ix_mapping_tenant_zoho", "tenant_id", "zoho_object_type", "zoho_object_id"),
    )


class ReconciliationRun(Base):
    """Trial Balance / Reconciliation run results."""
    __tablename__ = "reconciliation_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    run_type: Mapped[str] = mapped_column(String(32), default="TRIAL_BALANCE")  # TRIAL_BALANCE, OPEN_INVOICES, etc.
    period_from: Mapped[str] = mapped_column(String(32), nullable=False)  # YYYY-MM-DD
    period_to: Mapped[str] = mapped_column(String(32), nullable=False)    # YYYY-MM-DD
    status: Mapped[str] = mapped_column(String(32), default="RUNNING")  # RUNNING, COMPLETED, FAILED, MISMATCH
    tally_total_debit: Mapped[float] = mapped_column(default=0.0)
    tally_total_credit: Mapped[float] = mapped_column(default=0.0)
    zoho_total_debit: Mapped[float] = mapped_column(default=0.0)
    zoho_total_credit: Mapped[float] = mapped_column(default=0.0)
    mismatch_count: Mapped[int] = mapped_column(default=0)
    mismatch_details: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_recon_tenant_period", "tenant_id", "period_from", "period_to"),
    )
