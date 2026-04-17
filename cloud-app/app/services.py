import secrets
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditEvent, Connector, SyncJob, Tenant


def emit_audit(db: Session, *, tenant_id: str | None, category: str, action: str, payload: dict) -> None:
    db.add(AuditEvent(tenant_id=tenant_id, category=category, action=action, payload=payload))


def create_tenant(db: Session, name: str, zoho_org_id: str | None) -> Tenant:
    tenant = Tenant(name=name, zoho_org_id=zoho_org_id)
    db.add(tenant)
    db.flush()
    emit_audit(db, tenant_id=tenant.id, category="TENANT", action="TENANT_CREATED", payload={"name": name})
    db.commit()
    db.refresh(tenant)
    return tenant


def enroll_connector(db: Session, tenant_id: str) -> Connector:
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    connector = Connector(
        tenant_id=tenant_id,
        enrollment_token=secrets.token_urlsafe(24),
        secret=secrets.token_urlsafe(32),
    )
    db.add(connector)
    db.flush()
    emit_audit(
        db,
        tenant_id=tenant_id,
        category="CONNECTOR",
        action="CONNECTOR_ENROLLED",
        payload={"connector_id": connector.id},
    )
    db.commit()
    db.refresh(connector)
    return connector


def authenticate_connector(db: Session, connector_id: str, secret: str) -> Connector:
    connector = db.get(Connector, connector_id)
    if not connector or connector.secret != secret:
        raise HTTPException(status_code=401, detail="Invalid connector credentials")
    return connector


def heartbeat_connector(db: Session, connector_id: str, secret: str) -> dict:
    connector = authenticate_connector(db, connector_id, secret)
    connector.last_heartbeat_at = datetime.utcnow()
    connector.status = "ONLINE"
    emit_audit(
        db,
        tenant_id=connector.tenant_id,
        category="CONNECTOR",
        action="HEARTBEAT",
        payload={"connector_id": connector_id},
    )
    db.commit()
    return {"status": "ok", "at": connector.last_heartbeat_at.isoformat()}


def create_job(db: Session, data: dict) -> SyncJob:
    tenant = db.get(Tenant, data["tenant_id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    job = SyncJob(**data)
    db.add(job)
    db.flush()
    emit_audit(
        db,
        tenant_id=job.tenant_id,
        category="SYNC",
        action="JOB_CREATED",
        payload={"job_id": job.id, "object_type": job.object_type, "direction": job.direction},
    )
    db.commit()
    db.refresh(job)
    return job


def pull_jobs(db: Session, connector_id: str, secret: str, limit: int) -> list[SyncJob]:
    connector = authenticate_connector(db, connector_id, secret)
    jobs = (
        db.execute(
            select(SyncJob)
            .where(SyncJob.tenant_id == connector.tenant_id, SyncJob.status == "QUEUED")
            .order_by(SyncJob.created_at.asc())
            .limit(limit)
        )
        .scalars()
        .all()
    )

    now = datetime.utcnow()
    for job in jobs:
        job.status = "IN_PROGRESS"
        job.attempt += 1
        job.updated_at = now
    if jobs:
        emit_audit(
            db,
            tenant_id=connector.tenant_id,
            category="SYNC",
            action="JOBS_DISPATCHED",
            payload={"connector_id": connector_id, "count": len(jobs)},
        )
    db.commit()
    return jobs


def ack_job(db: Session, job_id: str, connector_id: str, secret: str) -> dict:
    connector = authenticate_connector(db, connector_id, secret)
    job = db.get(SyncJob, job_id)
    if not job or job.tenant_id != connector.tenant_id:
        raise HTTPException(status_code=404, detail="Job not found")
    job.status = "DONE"
    job.error_message = None
    emit_audit(db, tenant_id=job.tenant_id, category="SYNC", action="JOB_ACK", payload={"job_id": job.id})
    db.commit()
    return {"status": "acknowledged", "job_id": job_id}


def fail_job(db: Session, job_id: str, connector_id: str, secret: str, error_message: str) -> dict:
    connector = authenticate_connector(db, connector_id, secret)
    job = db.get(SyncJob, job_id)
    if not job or job.tenant_id != connector.tenant_id:
        raise HTTPException(status_code=404, detail="Job not found")

    job.error_message = error_message
    if job.attempt >= 4:
        job.status = "DEAD_LETTER"
    else:
        job.status = "QUEUED"

    emit_audit(
        db,
        tenant_id=job.tenant_id,
        category="SYNC",
        action="JOB_FAIL",
        payload={"job_id": job.id, "attempt": job.attempt, "status": job.status, "error_message": error_message},
    )
    db.commit()
    return {"status": job.status, "job_id": job_id}
