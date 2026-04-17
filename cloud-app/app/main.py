import secrets as py_secrets

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import settings
from .database import Base, engine, get_db
from .models import AuditEvent, SyncJob
from .schemas import (
    AuditOut,
    ConnectorEnrollmentOut,
    HeartbeatIn,
    JobAckIn,
    JobFailIn,
    PullJobsIn,
    SyncJobCreate,
    SyncJobOut,
    TenantCreate,
    TenantOut,
)
from .services import (
    ack_job,
    create_job,
    create_tenant,
    enroll_connector,
    fail_job,
    heartbeat_connector,
    pull_jobs,
    retry_job,
)


Base.metadata.create_all(bind=engine)


app = FastAPI(title="ZohoBooks2Tally Cloud App", version="0.2.1")


def verify_api_key(x_api_key: str = Header(default="")) -> None:
    if not py_secrets.compare_digest(x_api_key or "", settings.cloud_api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.post("/tenants", response_model=TenantOut, dependencies=[Depends(verify_api_key)])
def create_tenant_route(data: TenantCreate, db: Session = Depends(get_db)):
    return create_tenant(db, data.name, data.zoho_org_id)


@app.post(
    "/tenants/{tenant_id}/connector/enroll",
    response_model=ConnectorEnrollmentOut,
    dependencies=[Depends(verify_api_key)],
)
def enroll_connector_route(tenant_id: str, db: Session = Depends(get_db)):
    connector = enroll_connector(db, tenant_id)
    return {
        "connector_id": connector.id,
        "enrollment_token": connector.enrollment_token,
        "secret": connector.secret,
    }


@app.post("/agent/heartbeat", dependencies=[Depends(verify_api_key)])
def heartbeat_route(data: HeartbeatIn, db: Session = Depends(get_db)):
    return heartbeat_connector(db, data.connector_id, data.secret)


@app.post("/agent/jobs/pull", response_model=list[SyncJobOut], dependencies=[Depends(verify_api_key)])
def pull_jobs_route(data: PullJobsIn, db: Session = Depends(get_db)):
    return pull_jobs(db, data.connector_id, data.secret, data.limit)


@app.post("/agent/jobs/{job_id}/ack", dependencies=[Depends(verify_api_key)])
def ack_job_route(job_id: str, data: JobAckIn, db: Session = Depends(get_db)):
    return ack_job(db, job_id, data.connector_id, data.secret)


@app.post("/agent/jobs/{job_id}/fail", dependencies=[Depends(verify_api_key)])
def fail_job_route(job_id: str, data: JobFailIn, db: Session = Depends(get_db)):
    return fail_job(db, job_id, data.connector_id, data.secret, data.error_message)


@app.post("/sync/jobs", response_model=SyncJobOut, dependencies=[Depends(verify_api_key)])
def create_job_route(data: SyncJobCreate, db: Session = Depends(get_db)):
    return create_job(db, data.model_dump())


@app.get("/sync/jobs", response_model=list[SyncJobOut], dependencies=[Depends(verify_api_key)])
def list_jobs_route(db: Session = Depends(get_db)):
    return db.execute(select(SyncJob).order_by(SyncJob.created_at.desc())).scalars().all()


@app.post("/sync/jobs/{job_id}/retry", dependencies=[Depends(verify_api_key)])
def retry_job_route(job_id: str, db: Session = Depends(get_db)):
    return retry_job(db, job_id)


@app.post("/reconcile/run", dependencies=[Depends(verify_api_key)])
def reconcile_run() -> dict:
    return {
        "status": "started",
        "checks": [
            "customer_outstanding_balances",
            "vendor_outstanding_balances",
            "open_invoice_counts",
            "receipt_payment_totals",
            "journal_totals",
        ],
    }


@app.get("/audit/events", response_model=list[AuditOut], dependencies=[Depends(verify_api_key)])
def list_audit(db: Session = Depends(get_db)):
    return db.execute(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(500)).scalars().all()
