import json
import os
import secrets as py_secrets
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from .config import settings
from .tally_http import TallyClient, parse_tally_xml_response, extract_tally_objects, tally_voucher_to_sync_payload


app = FastAPI(title="Tally2ZohoBooks On-Prem Agent", version="5.7.0")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def verify_api_key(x_api_key: str = Header(default="")) -> None:
    if not py_secrets.compare_digest(x_api_key or "", settings.cloud_api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")


def _load_persisted_registration() -> None:
    store = Path(settings.agent_local_store)
    if settings.connector_id and settings.connector_secret:
        return
    if not store.exists():
        return
    try:
        data = json.loads(store.read_text())
        settings.connector_id = data.get("connector_id") or settings.connector_id
        settings.connector_secret = data.get("secret") or settings.connector_secret
    except (json.JSONDecodeError, OSError):
        return


def _persist_registration(connector_id: str, secret: str) -> None:
    store = Path(settings.agent_local_store)
    try:
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps({"connector_id": connector_id, "secret": secret}))
        os.chmod(store, 0o600)
    except OSError:
        return


_load_persisted_registration()


class RegistrationIn(BaseModel):
    connector_id: str
    secret: str


class TallyExportRequest(BaseModel):
    from_date: str = Query(pattern=r"^\d{4}-\d{2}-\d{2}$")
    to_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    object_types: list[str] | None = None


def assert_registered() -> tuple[str, str]:
    if not settings.connector_id or not settings.connector_secret:
        raise HTTPException(status_code=400, detail="Agent is not registered. Call /agent/register first.")
    return settings.connector_id, settings.connector_secret


def process_job_payload(job: dict) -> tuple[bool, str]:
    payload = job.get("payload") or {}
    if payload.get("force_fail"):
        return False, "Simulated Tally import failure for testing"
    return True, f"Processed {job.get('object_type')}:{job.get('source_id')}"


@app.get("/agent/health")
def health() -> dict:
    return {
        "status": "ok",
        "agent_name": settings.agent_name,
        "registered": bool(settings.connector_id),
        "time": utcnow_iso(),
    }


@app.post("/agent/register", dependencies=[Depends(verify_api_key)])
def register(data: RegistrationIn) -> dict:
    settings.connector_id = data.connector_id
    settings.connector_secret = data.secret
    _persist_registration(data.connector_id, data.secret)
    return {"status": "registered", "connector_id": settings.connector_id}


@app.post("/agent/run-once", dependencies=[Depends(verify_api_key)])
def run_once() -> dict:
    connector_id, secret = assert_registered()
    headers = {"x-api-key": settings.cloud_api_key}

    with httpx.Client(timeout=30.0) as client:
        hb = client.post(
            f"{settings.cloud_base_url}/agent/heartbeat",
            json={"connector_id": connector_id, "secret": secret},
            headers=headers,
        )
        hb.raise_for_status()

        pull = client.post(
            f"{settings.cloud_base_url}/agent/jobs/pull",
            json={"connector_id": connector_id, "secret": secret, "limit": settings.agent_poll_size},
            headers=headers,
        )
        pull.raise_for_status()
        jobs = pull.json()

        results = []
        for job in jobs:
            ok, message = process_job_payload(job)
            if ok:
                response = client.post(
                    f"{settings.cloud_base_url}/agent/jobs/{job['id']}/ack",
                    json={"connector_id": connector_id, "secret": secret},
                    headers=headers,
                )
            else:
                response = client.post(
                    f"{settings.cloud_base_url}/agent/jobs/{job['id']}/fail",
                    json={"connector_id": connector_id, "secret": secret, "error_message": message},
                    headers=headers,
                )
            response.raise_for_status()
            results.append({"job_id": job["id"], "result": response.json()})

    return {"pulled": len(jobs), "results": results}


# ==================== TALLY EXPORT ENDPOINTS ====================

@app.get("/tally/health", dependencies=[Depends(verify_api_key)])
async def tally_health() -> dict:
    """Check if Tally is reachable."""
    tally = TallyClient(settings.tally_base_url)
    healthy = await tally.health_check()
    await tally.close()
    return {"tally_reachable": healthy, "url": settings.tally_base_url}


@app.post("/tally/export/masters", dependencies=[Depends(verify_api_key)])
async def export_masters(request: TallyExportRequest) -> dict:
    """Export all masters (ledgers, stock items, groups, godowns, units) from Tally."""
    tally = TallyClient(settings.tally_base_url)
    
    try:
        results = {}
        # Export ledgers
        ledger_xml = await tally.export_ledgers(request.from_date)
        ledger_data = parse_tally_xml_response(ledger_xml)
        ledgers = extract_tally_objects(ledger_data, "LEDGER")
        results["ledgers"] = {"count": len(ledgers), "data": ledgers}
        
        # Export stock items
        stock_xml = await tally.export_stock_items(request.from_date)
        stock_data = parse_tally_xml_response(stock_xml)
        stock_items = extract_tally_objects(stock_data, "STOCKITEM")
        results["stock_items"] = {"count": len(stock_items), "data": stock_items}
        
        # Export groups
        group_xml = await tally.export_groups()
        group_data = parse_tally_xml_response(group_xml)
        groups = extract_tally_objects(group_data, "GROUP")
        results["groups"] = {"count": len(groups), "data": groups}
        
        # Export godowns
        godown_xml = await tally.export_godowns()
        godown_data = parse_tally_xml_response(godown_xml)
        godowns = extract_tally_objects(godown_data, "GODOWN")
        results["godowns"] = {"count": len(godowns), "data": godowns}
        
        # Export units
        unit_xml = await tally.export_units()
        unit_data = parse_tally_xml_response(unit_xml)
        units = extract_tally_objects(unit_data, "UNIT")
        results["units"] = {"count": len(units), "data": units}
        
        return {"success": True, "masters": results}
    finally:
        await tally.close()


@app.post("/tally/export/vouchers", dependencies=[Depends(verify_api_key)])
async def export_vouchers(request: TallyExportRequest) -> dict:
    """Export vouchers from Tally for a date range."""
    tally = TallyClient(settings.tally_base_url)
    
    try:
        results = {}
        voucher_xml = await tally.export_vouchers(request.from_date, request.to_date)
        voucher_data = parse_tally_xml_response(voucher_xml)
        vouchers = extract_tally_objects(voucher_data, "VOUCHER")
        
        # Convert to sync job payloads
        sync_jobs = []
        for voucher in vouchers:
            job_payload = tally_voucher_to_sync_payload(voucher)
            sync_jobs.append(job_payload)
        
        results["vouchers"] = {"count": len(vouchers), "raw_data": vouchers}
        results["sync_jobs"] = {"count": len(sync_jobs), "jobs": sync_jobs}
        
        return {"success": True, "vouchers": results}
    finally:
        await tally.close()


@app.post("/tally/export/sales", dependencies=[Depends(verify_api_key)])
async def export_sales_vouchers(request: TallyExportRequest) -> dict:
    """Export sales vouchers (invoices) from Tally."""
    tally = TallyClient(settings.tally_base_url)
    
    try:
        xml = await tally.export_sales_vouchers(request.from_date, request.to_date)
        data = parse_tally_xml_response(xml)
        vouchers = extract_tally_objects(data, "VOUCHER")
        
        sync_jobs = [tally_voucher_to_sync_payload(v) for v in vouchers]
        
        return {"success": True, "count": len(vouchers), "sync_jobs": sync_jobs}
    finally:
        await tally.close()


@app.post("/tally/export/purchases", dependencies=[Depends(verify_api_key)])
async def export_purchase_vouchers(request: TallyExportRequest) -> dict:
    """Export purchase vouchers (bills) from Tally."""
    tally = TallyClient(settings.tally_base_url)
    
    try:
        xml = await tally.export_purchase_vouchers(request.from_date, request.to_date)
        data = parse_tally_xml_response(xml)
        vouchers = extract_tally_objects(data, "VOUCHER")
        
        sync_jobs = [tally_voucher_to_sync_payload(v) for v in vouchers]
        
        return {"success": True, "count": len(vouchers), "sync_jobs": sync_jobs}
    finally:
        await tally.close()


@app.post("/tally/export/payments", dependencies=[Depends(verify_api_key)])
async def export_payment_vouchers(request: TallyExportRequest) -> dict:
    """Export payment vouchers from Tally."""
    tally = TallyClient(settings.tally_base_url)
    
    try:
        xml = await tally.export_payment_vouchers(request.from_date, request.to_date)
        data = parse_tally_xml_response(xml)
        vouchers = extract_tally_objects(data, "VOUCHER")
        
        sync_jobs = [tally_voucher_to_sync_payload(v) for v in vouchers]
        
        return {"success": True, "count": len(vouchers), "sync_jobs": sync_jobs}
    finally:
        await tally.close()


@app.post("/tally/export/receipts", dependencies=[Depends(verify_api_key)])
async def export_receipt_vouchers(request: TallyExportRequest) -> dict:
    """Export receipt vouchers from Tally."""
    tally = TallyClient(settings.tally_base_url)
    
    try:
        xml = await tally.export_receipt_vouchers(request.from_date, request.to_date)
        data = parse_tally_xml_response(xml)
        vouchers = extract_tally_objects(data, "VOUCHER")
        
        sync_jobs = [tally_voucher_to_sync_payload(v) for v in vouchers]
        
        return {"success": True, "count": len(vouchers), "sync_jobs": sync_jobs}
    finally:
        await tally.close()


@app.post("/tally/export/journals", dependencies=[Depends(verify_api_key)])
async def export_journal_vouchers(request: TallyExportRequest) -> dict:
    """Export journal vouchers from Tally."""
    tally = TallyClient(settings.tally_base_url)
    
    try:
        xml = await tally.export_journal_vouchers(request.from_date, request.to_date)
        data = parse_tally_xml_response(xml)
        vouchers = extract_tally_objects(data, "VOUCHER")
        
        sync_jobs = [tally_voucher_to_sync_payload(v) for v in vouchers]
        
        return {"success": True, "count": len(vouchers), "sync_jobs": sync_jobs}
    finally:
        await tally.close()


@app.post("/tally/export/trial-balance", dependencies=[Depends(verify_api_key)])
async def export_trial_balance(request: TallyExportRequest) -> dict:
    """Export trial balance from Tally."""
    tally = TallyClient(settings.tally_base_url)
    
    try:
        xml = await tally.export_trial_balance(request.from_date, request.to_date)
        data = parse_tally_xml_response(xml)
        tb_entries = extract_tally_objects(data, "LEDGER")
        
        # Transform trial balance entries
        tb_payload = []
        for entry in tb_entries:
            tb_payload.append({
                "ledger_name": entry.get("NAME", ""),
                "debit": float(entry.get("DEBIT", 0) or 0),
                "credit": float(entry.get("CREDIT", 0) or 0),
            })
        
        return {"success": True, "count": len(tb_payload), "trial_balance": tb_payload}
    finally:
        await tally.close()


@app.post("/tally/export/outstandings", dependencies=[Depends(verify_api_key)])
async def export_outstandings(request: TallyExportRequest) -> dict:
    """Export outstanding receivables and payables from Tally."""
    tally = TallyClient(settings.tally_base_url)
    
    try:
        # Receivables
        rec_xml = await tally.export_outstanding_receivables(request.to_date or request.from_date)
        rec_data = parse_tally_xml_response(rec_xml)
        receivables = extract_tally_objects(rec_data, "LEDGER")
        
        # Payables
        pay_xml = await tally.export_outstanding_payables(request.to_date or request.from_date)
        pay_data = parse_tally_xml_response(pay_xml)
        payables = extract_tally_objects(pay_data, "LEDGER")
        
        return {
            "success": True,
            "receivables": {"count": len(receivables), "data": receivables},
            "payables": {"count": len(payables), "data": payables},
        }
    finally:
        await tally.close()


@app.post("/tally/export/incremental", dependencies=[Depends(verify_api_key)])
async def export_incremental(request: TallyExportRequest) -> dict:
    """Export objects changed since a specific date (incremental sync)."""
    tally = TallyClient(settings.tally_base_url)
    
    try:
        results = {}
        since = request.from_date  # Use from_date as "since" date
        
        # Changed ledgers
        ledger_xml = await tally.export_changed_ledgers(since)
        ledger_data = parse_tally_xml_response(ledger_xml)
        ledgers = extract_tally_objects(ledger_data, "LEDGER")
        results["ledgers"] = {"count": len(ledgers), "data": ledgers}
        
        # Changed stock items
        stock_xml = await tally.export_changed_stock_items(since)
        stock_data = parse_tally_xml_response(stock_xml)
        stock_items = extract_tally_objects(stock_data, "STOCKITEM")
        results["stock_items"] = {"count": len(stock_items), "data": stock_items}
        
        # Changed vouchers
        voucher_xml = await tally.export_changed_vouchers(since)
        voucher_data = parse_tally_xml_response(voucher_xml)
        vouchers = extract_tally_objects(voucher_data, "VOUCHER")
        
        sync_jobs = [tally_voucher_to_sync_payload(v) for v in vouchers]
        results["vouchers"] = {"count": len(vouchers), "data": vouchers}
        results["sync_jobs"] = {"count": len(sync_jobs), "jobs": sync_jobs}
        
        return {"success": True, "since": since, "results": results}
    finally:
        await tally.close()


@app.post("/tally/sync-to-cloud", dependencies=[Depends(verify_api_key)])
async def sync_tally_to_cloud(
    from_date: str = Query(pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    object_types: list[str] | None = Query(default=None),
) -> dict:
    """Full sync: Export from Tally and push sync jobs to cloud."""
    connector_id, secret = assert_registered()
    headers = {"x-api-key": settings.cloud_api_key}
    
    tally = TallyClient(settings.tally_base_url)
    cloud_base = settings.cloud_base_url
    
    try:
        to_date = to_date or from_date
        all_sync_jobs = []
        
        # Export masters first (dependencies)
        if not object_types or "masters" in object_types:
            # Ledgers -> Contacts
            ledger_xml = await tally.export_ledgers(from_date)
            ledger_data = parse_tally_xml_response(ledger_xml)
            ledgers = extract_tally_objects(ledger_data, "LEDGER")
            
            for ledger in ledgers:
                payload = {
                    "object_type": "CONTACT",
                    "source_id": ledger.get("GUID", ledger.get("NAME", "")),
                    "direction": "TALLY_TO_ZOHO",
                    "payload": {
                        "name": ledger.get("NAME", ""),
                        "tally_guid": ledger.get("GUID", ""),
                        "is_customer": "SUNDRY DEBTOR" in ledger.get("PARENT", "").upper(),
                        "contact_type": "customer" if "SUNDRY DEBTOR" in ledger.get("PARENT", "").upper() else "vendor",
                        "gstin": ledger.get("GSTIN", ""),
                        "pan": ledger.get("PAN", ""),
                        "address": ledger.get("MAILINGDETAILS", {}).get("ADDRESS", ""),
                        "city": ledger.get("MAILINGDETAILS", {}).get("CITY", ""),
                        "state": ledger.get("MAILINGDETAILS", {}).get("STATE", ""),
                        "pincode": ledger.get("MAILINGDETAILS", {}).get("PINCODE", ""),
                        "country": ledger.get("MAILINGDETAILS", {}).get("COUNTRY", "India"),
                        "email": ledger.get("MAILINGDETAILS", {}).get("EMAIL", ""),
                        "phone": ledger.get("MAILINGDETAILS", {}).get("PHONE", ""),
                        "mobile": ledger.get("MAILINGDETAILS", {}).get("MOBILE", ""),
                        "contact_person": ledger.get("MAILINGDETAILS", {}).get("CONTACTPERSON", ""),
                        "opening_balance": float(ledger.get("OPENINGBALANCE", 0) or 0),
                        "credit_period": int(ledger.get("CREDITPERIOD", 0) or 0),
                        "currency": ledger.get("CURRENCYNAME", "INR"),
                    }
                }
                all_sync_jobs.append(payload)
            
            # Stock Items -> Items
            stock_xml = await tally.export_stock_items(from_date)
            stock_data = parse_tally_xml_response(stock_xml)
            stock_items = extract_tally_objects(stock_data, "STOCKITEM")
            
            for item in stock_items:
                gst = item.get("GSTDETAILS", {})
                payload = {
                    "object_type": "ITEM",
                    "source_id": item.get("GUID", item.get("NAME", "")),
                    "direction": "TALLY_TO_ZOHO",
                    "payload": {
                        "name": item.get("NAME", ""),
                        "tally_guid": item.get("GUID", ""),
                        "description": item.get("DESCRIPTION", ""),
                        "unit": item.get("BASEUNITS", "Nos"),
                        "rate": float(item.get("STANDARDRATE", 0) or 0),
                        "purchase_rate": float(item.get("PURCHASERATE", 0) or 0),
                        "tax_id": gst.get("TAXID", ""),
                        "tax_name": gst.get("TAXNAME", ""),
                        "tax_percentage": float(gst.get("TAXPERCENTAGE", 0) or 0),
                        "hsn_code": gst.get("HSNCODE", ""),
                        "sac_code": gst.get("SACCODE", ""),
                        "is_inventory": item.get("ISSTOCKITEM", "Yes") == "Yes",
                        "reorder_level": float(item.get("REORDERLEVEL", 0) or 0),
                        "sku": item.get("SKU", ""),
                    }
                }
                all_sync_jobs.append(payload)
        
        # Export vouchers
        if not object_types or "vouchers" in object_types:
            voucher_xml = await tally.export_vouchers(from_date, to_date)
            voucher_data = parse_tally_xml_response(voucher_xml)
            vouchers = extract_tally_objects(voucher_data, "VOUCHER")
            
            for voucher in vouchers:
                job_payload = tally_voucher_to_sync_payload(voucher)
                job_payload["direction"] = "TALLY_TO_ZOHO"
                all_sync_jobs.append(job_payload)
        
        # Push all jobs to cloud
        created_jobs = []
        failed_jobs = []
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            for job_payload in all_sync_jobs:
                try:
                    resp = await client.post(
                        f"{cloud_base}/sync/jobs",
                        json=job_payload,
                        headers=headers,
                    )
                    if resp.status_code == 200:
                        created_jobs.append(resp.json())
                    elif resp.status_code == 409:
                        # Already exists - skip
                        pass
                    else:
                        failed_jobs.append({"payload": job_payload, "error": resp.text})
                except Exception as e:
                    failed_jobs.append({"payload": job_payload, "error": str(e)})
        
        return {
            "success": True,
            "period": {"from": from_date, "to": to_date},
            "total_exported": len(all_sync_jobs),
            "jobs_created": len(created_jobs),
            "jobs_failed": len(failed_jobs),
            "failed_details": failed_jobs[:10] if failed_jobs else [],
        }
    finally:
        await tally.close()