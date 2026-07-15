import secrets as py_secrets
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import settings
from .database import Base, engine, get_db
from .models import AuditEvent, ReconciliationRun, SyncJob, Tenant, TallyZohoMapping, ZohoToken
from .schemas import (
    AuditOut,
    ConnectorEnrollmentOut,
    HeartbeatIn,
    JobAckIn,
    JobFailIn,
    OpenInvoicesReconcileIn,
    PaymentsReconcileIn,
    PullJobsIn,
    ReconciliationRunOut,
    SyncJobCreate,
    SyncJobOut,
    TenantCreate,
    TenantOut,
    TrialBalanceReconcileIn,
    TallyZohoMappingOut,
    ZohoOAuthTokenOut,
    ZohoOAuthURLOut,
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
from .reconciliation import ReconciliationService
from .zoho_client import ZohoBooksClient


Base.metadata.create_all(bind=engine)


app = FastAPI(title="ZohoBooks2Tally Cloud App", version="0.3.0")


def verify_api_key(x_api_key: str = Header(default="")) -> None:
    if not py_secrets.compare_digest(x_api_key or "", settings.cloud_api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


# ============ TENANT MANAGEMENT ============

@app.post("/tenants", response_model=TenantOut, dependencies=[Depends(verify_api_key)])
def create_tenant_route(data: TenantCreate, db: Session = Depends(get_db)):
    return create_tenant(db, data.name, data.zoho_org_id)


@app.get("/tenants", response_model=list[TenantOut], dependencies=[Depends(verify_api_key)])
def list_tenants(db: Session = Depends(get_db)):
    return db.execute(select(Tenant).order_by(Tenant.created_at.desc())).scalars().all()


@app.get("/tenants/{tenant_id}", response_model=TenantOut, dependencies=[Depends(verify_api_key)])
def get_tenant(tenant_id: str, db: Session = Depends(get_db)):
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


# ============ CONNECTOR ENROLLMENT ============

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


# ============ AGENT ENDPOINTS ============

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


# ============ SYNC JOBS ============

@app.post("/sync/jobs", response_model=SyncJobOut, dependencies=[Depends(verify_api_key)])
def create_job_route(data: SyncJobCreate, db: Session = Depends(get_db)):
    return create_job(db, data.model_dump())


@app.get("/sync/jobs", response_model=list[SyncJobOut], dependencies=[Depends(verify_api_key)])
def list_jobs_route(db: Session = Depends(get_db)):
    return db.execute(select(SyncJob).order_by(SyncJob.created_at.desc())).scalars().all()


@app.get("/sync/jobs/{job_id}", response_model=SyncJobOut, dependencies=[Depends(verify_api_key)])
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(SyncJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/sync/jobs/{job_id}/retry", dependencies=[Depends(verify_api_key)])
def retry_job_route(job_id: str, db: Session = Depends(get_db)):
    return retry_job(db, job_id)


@app.post("/sync/jobs/{job_id}/process", dependencies=[Depends(verify_api_key)])
async def process_job(job_id: str, db: Session = Depends(get_db)):
    """Process a single TALLY_TO_ZOHO sync job."""
    from .sync_processor import process_sync_job
    return await process_sync_job(db, job_id)


@app.post("/sync/jobs/batch/process", dependencies=[Depends(verify_api_key)])
async def process_jobs_batch(
    tenant_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Process all pending TALLY_TO_ZOHO jobs for a tenant."""
    from .sync_processor import SyncProcessor, ZohoBooksClient
    
    tenant = db.get(Tenant, tenant_id)
    if not tenant or not tenant.zoho_org_id:
        raise HTTPException(status_code=404, detail="Tenant not found or missing Zoho org ID")
    
    zoho_client = ZohoBooksClient(db, tenant)
    processor = SyncProcessor(db, tenant_id)
    await processor.initialize(zoho_client)
    
    jobs = db.execute(
        select(SyncJob).where(
            SyncJob.tenant_id == tenant_id,
            SyncJob.direction == "TALLY_TO_ZOHO",
            SyncJob.status == "QUEUED",
        ).order_by(SyncJob.priority.desc(), SyncJob.created_at.asc()).limit(limit)
    ).scalars().all()
    
    results = []
    for job in jobs:
        job.status = "IN_PROGRESS"
        job.attempt += 1
        db.commit()
        
        try:
            result = await processor.process_job(job)
            job.status = "DONE"
            job.error_message = None
            results.append({"job_id": job.id, "status": "success", "result": result})
        except Exception as e:
            job.error_message = str(e)
            if job.attempt >= 5:
                job.status = "DEAD_LETTER"
            else:
                job.status = "QUEUED"
            results.append({"job_id": job.id, "status": "failed", "error": str(e)})
        
        db.commit()
    
    return {"processed": len(results), "results": results}


# ============ ZOHO OAUTH ============

@app.get("/oauth/zoho/url", response_model=ZohoOAuthURLOut, dependencies=[Depends(verify_api_key)])
def get_zoho_oauth_url(state: str | None = None):
    """Get Zoho Books OAuth authorization URL for India edition."""
    auth_url = ZohoBooksClient.get_oauth_url(state)
    return {"auth_url": auth_url}


@app.get("/oauth/zoho/callback", response_model=ZohoOAuthTokenOut)
async def zoho_oauth_callback(code: str, state: str | None = None, db: Session = Depends(get_db)):
    """Handle Zoho OAuth callback and store tokens."""
    token_data = await ZohoBooksClient.exchange_code_for_tokens(code)
    
    # If state contains tenant_id, associate tokens with tenant
    if state and state.startswith("tenant_"):
        tenant_id = state.replace("tenant_", "")
        tenant = db.get(Tenant, tenant_id)
        if tenant:
            expires_at = int(datetime.now(timezone.utc).timestamp()) + token_data.get("expires_in", 3600)
            
            zoho_token = db.query(ZohoToken).filter(ZohoToken.tenant_id == tenant_id).first()
            if zoho_token:
                zoho_token.access_token = token_data["access_token"]
                zoho_token.refresh_token = token_data["refresh_token"]
                zoho_token.expires_at = expires_at
                zoho_token.scope = token_data.get("scope", "")
                zoho_token.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            else:
                zoho_token = ZohoToken(
                    tenant_id=tenant_id,
                    access_token=token_data["access_token"],
                    refresh_token=token_data["refresh_token"],
                    expires_at=expires_at,
                    scope=token_data.get("scope", ""),
                )
                db.add(zoho_token)
            
            db.commit()
    
    return token_data


@app.post("/tenants/{tenant_id}/zoho/connect", dependencies=[Depends(verify_api_key)])
def initiate_zoho_connect(tenant_id: str, db: Session = Depends(get_db)):
    """Get OAuth URL for a specific tenant."""
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    
    state = f"tenant_{tenant_id}"
    auth_url = ZohoBooksClient.get_oauth_url(state)
    return {"auth_url": auth_url, "state": state}


# ============ RECONCILIATION ============

@app.post("/reconcile/trial-balance", response_model=ReconciliationRunOut, dependencies=[Depends(verify_api_key)])
async def reconcile_trial_balance(data: TrialBalanceReconcileIn, db: Session = Depends(get_db)):
    """Reconcile Trial Balance between Tally and Zoho Books."""
    tenant = db.get(Tenant, data.tenant_id)
    if not tenant or not tenant.zoho_org_id:
        raise HTTPException(status_code=404, detail="Tenant not found or missing Zoho org ID")
    
    zoho_client = ZohoBooksClient(db, tenant)
    
    # Convert Tally trial balance list to dict format expected by reconciliation
    tally_tb = {}
    for item in data.tally_trial_balance:
        tally_tb[item.ledger_name] = {"debit": item.debit, "credit": item.credit}
    
    service = ReconciliationService(db)
    run = await service.run_trial_balance_reconciliation(
        tenant_id=data.tenant_id,
        period_from=data.period_from,
        period_to=data.period_to,
        zoho_client=zoho_client,
        tally_trial_balance=tally_tb,
    )
    
    return run


@app.post("/reconcile/open-invoices", response_model=ReconciliationRunOut, dependencies=[Depends(verify_api_key)])
async def reconcile_open_invoices(data: OpenInvoicesReconcileIn, db: Session = Depends(get_db)):
    """Reconcile open invoices between Tally and Zoho Books."""
    tenant = db.get(Tenant, data.tenant_id)
    if not tenant or not tenant.zoho_org_id:
        raise HTTPException(status_code=404, detail="Tenant not found or missing Zoho org ID")

    zoho_client = ZohoBooksClient(db, tenant)
    service = ReconciliationService(db)
    return await service.run_open_invoices_reconciliation(
        tenant_id=data.tenant_id,
        period_from=data.period_from,
        period_to=data.period_to,
        zoho_client=zoho_client,
        tally_open_invoices=data.tally_open_invoices,
    )


@app.post("/reconcile/payments", response_model=ReconciliationRunOut, dependencies=[Depends(verify_api_key)])
async def reconcile_payments(data: PaymentsReconcileIn, db: Session = Depends(get_db)):
    """Reconcile payments/receipts between Tally and Zoho Books."""
    tenant = db.get(Tenant, data.tenant_id)
    if not tenant or not tenant.zoho_org_id:
        raise HTTPException(status_code=404, detail="Tenant not found or missing Zoho org ID")

    zoho_client = ZohoBooksClient(db, tenant)
    service = ReconciliationService(db)
    return await service.run_payments_reconciliation(
        tenant_id=data.tenant_id,
        period_from=data.period_from,
        period_to=data.period_to,
        zoho_client=zoho_client,
        tally_payments=data.tally_payments,
    )


@app.get("/reconcile/runs", response_model=list[ReconciliationRunOut], dependencies=[Depends(verify_api_key)])
def list_reconciliation_runs(
    tenant_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    query = select(ReconciliationRun).order_by(ReconciliationRun.started_at.desc())
    if tenant_id:
        query = query.where(ReconciliationRun.tenant_id == tenant_id)
    query = query.limit(limit)
    return db.execute(query).scalars().all()


@app.get("/reconcile/runs/{run_id}", response_model=ReconciliationRunOut, dependencies=[Depends(verify_api_key)])
def get_reconciliation_run(run_id: str, db: Session = Depends(get_db)):
    run = db.get(ReconciliationRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Reconciliation run not found")
    return run


# ============ TALLY-ZOHO MAPPINGS ============

@app.get("/mappings", response_model=list[TallyZohoMappingOut], dependencies=[Depends(verify_api_key)])
def list_mappings(
    tenant_id: str | None = None,
    tally_type: str | None = None,
    zoho_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    query = select(TallyZohoMapping).order_by(TallyZohoMapping.created_at.desc())
    if tenant_id:
        query = query.where(TallyZohoMapping.tenant_id == tenant_id)
    if tally_type:
        query = query.where(TallyZohoMapping.tally_object_type == tally_type)
    if zoho_type:
        query = query.where(TallyZohoMapping.zoho_object_type == zoho_type)
    query = query.limit(limit)
    return db.execute(query).scalars().all()


@app.post("/mappings", response_model=TallyZohoMappingOut, dependencies=[Depends(verify_api_key)])
def create_mapping(
    tenant_id: str,
    tally_object_type: str,
    tally_object_id: str,
    tally_object_name: str | None,
    zoho_object_type: str,
    zoho_object_id: str,
    zoho_object_name: str | None,
    db: Session = Depends(get_db),
):
    """Manually create a Tally-Zoho mapping."""
    mapping = TallyZohoMapping(
        tenant_id=tenant_id,
        tally_object_type=tally_object_type,
        tally_object_id=tally_object_id,
        tally_object_name=tally_object_name,
        zoho_object_type=zoho_object_type,
        zoho_object_id=zoho_object_id,
        zoho_object_name=zoho_object_name,
    )
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return mapping


@app.delete("/mappings/{mapping_id}", dependencies=[Depends(verify_api_key)])
def delete_mapping(mapping_id: str, db: Session = Depends(get_db)):
    mapping = db.get(TallyZohoMapping, mapping_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found")
    db.delete(mapping)
    db.commit()
    return {"status": "deleted"}


# ============ AUDIT ============

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