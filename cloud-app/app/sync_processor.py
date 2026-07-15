"""
Sync Processor - Handles Tally -> Zoho Books synchronization logic.

Consumes the normalized sync-job payloads emitted by the on-prem agent and
maps them to Zoho Books API calls via the canonical mappers in ``tally_mapper``.

Idempotency model
-----------------
Every Tally object is tracked in ``TallyZohoMapping`` keyed by
``(tenant_id, tally_object_type, tally_object_id=tally_guid)``. Documents also
carry ``cf_external_id = tally_guid`` in Zoho itself. A content ``sync_hash``
lets unchanged masters be skipped and changed masters be updated in place, so a
re-run never double-posts.
"""
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import AuditEvent, SyncJob, TallyZohoMapping, Tenant
from .zoho_client import ZohoBooksClient
from . import tally_mapper as tm


MAX_ATTEMPTS = 5


class SyncProcessor:
    """Processes sync jobs for the Tally -> Zoho Books direction."""

    def __init__(self, db: Session, tenant_id: str):
        self.db = db
        self.tenant_id = tenant_id
        self.zoho_client: ZohoBooksClient | None = None

        # Resolution caches. Keyed independently so a party ledger name and a
        # stock item name that happen to collide never cross-resolve.
        self._by_guid: dict[tuple[str, str], dict] = {}   # (tally_type, guid)  -> mapping info
        self._by_name: dict[tuple[str, str], dict] = {}    # (tally_type, name) -> mapping info

    async def initialize(self, zoho_client: ZohoBooksClient) -> None:
        self.zoho_client = zoho_client
        self._load_mapping_caches()

    def _load_mapping_caches(self) -> None:
        mappings = self.db.query(TallyZohoMapping).filter(
            TallyZohoMapping.tenant_id == self.tenant_id
        ).all()
        for m in mappings:
            info = {
                "zoho_id": m.zoho_object_id,
                "zoho_name": m.zoho_object_name,
                "zoho_type": m.zoho_object_type,
                "tally_name": m.tally_object_name,
            }
            self._by_guid[(m.tally_object_type, m.tally_object_id)] = info
            if m.tally_object_name:
                self._by_name[(m.tally_object_type, m.tally_object_name)] = info

    def _get_by_guid(self, tally_type: str, guid: str) -> dict | None:
        return self._by_guid.get((tally_type, guid))

    def _resolve_zoho_id_by_name(self, tally_type: str, name: str) -> str | None:
        info = self._by_name.get((tally_type, name))
        return info["zoho_id"] if info else None

    def _set_mapping(
        self,
        tally_type: str,
        tally_id: str,
        tally_name: str,
        zoho_type: str,
        zoho_id: str,
        zoho_name: str,
        sync_hash: str | None = None,
    ) -> None:
        info = {"zoho_id": zoho_id, "zoho_name": zoho_name, "zoho_type": zoho_type, "tally_name": tally_name}
        self._by_guid[(tally_type, tally_id)] = info
        if tally_name:
            self._by_name[(tally_type, tally_name)] = info

        mapping = self.db.query(TallyZohoMapping).filter(
            TallyZohoMapping.tenant_id == self.tenant_id,
            TallyZohoMapping.tally_object_type == tally_type,
            TallyZohoMapping.tally_object_id == tally_id,
        ).first()

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if mapping:
            mapping.zoho_object_type = zoho_type
            mapping.zoho_object_id = zoho_id
            mapping.zoho_object_name = zoho_name
            mapping.tally_object_name = tally_name
            mapping.last_synced_at = now
            if sync_hash is not None:
                mapping.sync_hash = sync_hash
        else:
            mapping = TallyZohoMapping(
                tenant_id=self.tenant_id,
                tally_object_type=tally_type,
                tally_object_id=tally_id,
                tally_object_name=tally_name,
                zoho_object_type=zoho_type,
                zoho_object_id=zoho_id,
                zoho_object_name=zoho_name,
                last_synced_at=now,
                sync_hash=sync_hash,
            )
            self.db.add(mapping)
        self.db.commit()

    def _compute_sync_hash(self, payload: dict) -> str:
        normalized = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(normalized.encode()).hexdigest()[:32]

    def _stored_hash(self, tally_type: str, guid: str) -> str | None:
        mapping = self.db.query(TallyZohoMapping).filter(
            TallyZohoMapping.tenant_id == self.tenant_id,
            TallyZohoMapping.tally_object_type == tally_type,
            TallyZohoMapping.tally_object_id == guid,
        ).first()
        return mapping.sync_hash if mapping else None

    async def process_job(self, job: SyncJob) -> dict:
        if self.zoho_client is None:
            raise RuntimeError("SyncProcessor not initialized. Call initialize() first.")

        handlers = {
            "CONTACT": self._sync_contact,
            "ITEM": self._sync_item,
            "INVOICE": self._sync_invoice,
            "BILL": self._sync_bill,
            "PAYMENT": self._sync_payment,
            "RECEIPT": self._sync_receipt,
            "JOURNAL": self._sync_journal,
            "CREDIT_NOTE": self._sync_credit_note,
            "DEBIT_NOTE": self._sync_debit_note,
            "LEDGER": self._sync_ledger,
        }
        handler = handlers.get(job.object_type)
        if handler is None:
            raise ValueError(f"Unknown object type: {job.object_type}")

        try:
            return await handler(self.zoho_client, job, job.payload or {})
        except Exception as e:
            self._emit_audit("SYNC", f"JOB_FAILED_{job.object_type}", {
                "job_id": job.id,
                "error": str(e),
                "object_type": job.object_type,
                "source_id": job.source_id,
            })
            raise

    # ------------------------------------------------------------------ #
    # Masters
    # ------------------------------------------------------------------ #

    async def _sync_contact(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        tally_guid = payload.get("tally_guid", job.source_id)
        ledger_name = payload.get("name", "")
        contact_type = "customer" if payload.get("is_customer", True) else "vendor"
        zoho_data = tm.map_contact(payload, contact_type)
        current_hash = self._compute_sync_hash(payload)

        existing = self._get_by_guid("LEDGER", tally_guid)
        if existing:
            if self._stored_hash("LEDGER", tally_guid) == current_hash:
                return {"status": "SKIPPED", "reason": "No changes", "zoho_id": existing["zoho_id"]}
            result = await client.update_contact(existing["zoho_id"], zoho_data)
            zoho = result["contact"]
            self._set_mapping("LEDGER", tally_guid, ledger_name, "CONTACT",
                              zoho["contact_id"], zoho["contact_name"], current_hash)
            return {"status": "UPDATED", "zoho_id": zoho["contact_id"]}

        result = await client.create_contact(zoho_data)
        zoho = result["contact"]
        self._set_mapping("LEDGER", tally_guid, ledger_name, "CONTACT",
                          zoho["contact_id"], zoho["contact_name"], current_hash)
        self._emit_audit("SYNC", "CONTACT_CREATED", {
            "job_id": job.id, "tally_guid": tally_guid,
            "zoho_id": zoho["contact_id"], "contact_type": contact_type,
        })
        return {"status": "CREATED", "zoho_id": zoho["contact_id"]}

    async def _sync_item(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        tally_guid = payload.get("tally_guid", job.source_id)
        item_name = payload.get("name", "")
        zoho_data = tm.map_item(payload)
        current_hash = self._compute_sync_hash(payload)

        existing = self._get_by_guid("STOCKITEM", tally_guid)
        if existing:
            if self._stored_hash("STOCKITEM", tally_guid) == current_hash:
                return {"status": "SKIPPED", "reason": "No changes", "zoho_id": existing["zoho_id"]}
            result = await client.update_item(existing["zoho_id"], zoho_data)
            zoho = result["item"]
            self._set_mapping("STOCKITEM", tally_guid, item_name, "ITEM",
                              zoho["item_id"], zoho["name"], current_hash)
            return {"status": "UPDATED", "zoho_id": zoho["item_id"]}

        result = await client.create_item(zoho_data)
        zoho = result["item"]
        self._set_mapping("STOCKITEM", tally_guid, item_name, "ITEM",
                          zoho["item_id"], zoho["name"], current_hash)
        self._emit_audit("SYNC", "ITEM_CREATED", {
            "job_id": job.id, "tally_guid": tally_guid, "zoho_id": zoho["item_id"],
        })
        return {"status": "CREATED", "zoho_id": zoho["item_id"]}

    async def _sync_ledger(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        tally_guid = payload.get("tally_guid", job.source_id)
        ledger_name = payload.get("name", "")

        existing = self._get_by_guid("LEDGER", tally_guid)
        if existing:
            return {"status": "SKIPPED", "reason": "Already synced", "zoho_id": existing["zoho_id"]}

        result = await client.create_account(tm.map_account(payload))
        zoho = result["chartofaccount"]
        self._set_mapping("LEDGER", tally_guid, ledger_name, "ACCOUNT",
                          zoho["account_id"], ledger_name, self._compute_sync_hash(payload))
        return {"status": "CREATED", "zoho_id": zoho["account_id"]}

    # ------------------------------------------------------------------ #
    # Transactions
    # ------------------------------------------------------------------ #

    def _item_ids(self, payload: dict) -> dict[str, str]:
        """Resolve inventory-line stock item names to Zoho item ids."""
        resolved: dict[str, str] = {}
        for entry in payload.get("inventory_entries", []):
            name = entry.get("stock_item_name", "")
            if name and name not in resolved:
                zoho_id = self._resolve_zoho_id_by_name("STOCKITEM", name)
                if zoho_id:
                    resolved[name] = zoho_id
        return resolved

    async def _sync_invoice(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        tally_guid = payload.get("tally_guid", job.source_id)
        voucher_number = payload.get("voucher_number", "")
        party_name = payload.get("party_ledger_name", "")
        customer_id = self._resolve_zoho_id_by_name("LEDGER", party_name)
        if not customer_id:
            raise ValueError(f"Customer not mapped in Zoho, sync the ledger first: {party_name!r}")

        zoho_data = tm.map_invoice(payload, customer_id, self._item_ids(payload))
        current_hash = self._compute_sync_hash(payload)

        existing = self._get_by_guid("VOUCHER", tally_guid)
        if existing:
            if self._stored_hash("VOUCHER", tally_guid) == current_hash:
                return {"status": "SKIPPED", "reason": "No changes", "zoho_id": existing["zoho_id"]}
            result = await client.update_invoice(existing["zoho_id"], zoho_data)
            zoho_id = result["invoice"]["invoice_id"]
            self._set_mapping("VOUCHER", tally_guid, voucher_number, "INVOICE", zoho_id, voucher_number, current_hash)
            return {"status": "UPDATED", "zoho_id": zoho_id}

        result = await client.create_invoice(zoho_data)
        zoho_id = result["invoice"]["invoice_id"]
        self._set_mapping("VOUCHER", tally_guid, voucher_number, "INVOICE", zoho_id, voucher_number, current_hash)
        self._emit_audit("SYNC", "INVOICE_CREATED", {
            "job_id": job.id, "tally_guid": tally_guid, "zoho_id": zoho_id, "voucher_number": voucher_number,
        })
        return {"status": "CREATED", "zoho_id": zoho_id}

    async def _sync_bill(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        tally_guid = payload.get("tally_guid", job.source_id)
        voucher_number = payload.get("voucher_number", "")
        party_name = payload.get("party_ledger_name", "")
        vendor_id = self._resolve_zoho_id_by_name("LEDGER", party_name)
        if not vendor_id:
            raise ValueError(f"Vendor not mapped in Zoho, sync the ledger first: {party_name!r}")

        zoho_data = tm.map_bill(payload, vendor_id, self._item_ids(payload))
        current_hash = self._compute_sync_hash(payload)

        existing = self._get_by_guid("VOUCHER", tally_guid)
        if existing:
            if self._stored_hash("VOUCHER", tally_guid) == current_hash:
                return {"status": "SKIPPED", "reason": "No changes", "zoho_id": existing["zoho_id"]}
            result = await client.update_bill(existing["zoho_id"], zoho_data)
            zoho_id = result["bill"]["bill_id"]
            self._set_mapping("VOUCHER", tally_guid, voucher_number, "BILL", zoho_id, voucher_number, current_hash)
            return {"status": "UPDATED", "zoho_id": zoho_id}

        result = await client.create_bill(zoho_data)
        zoho_id = result["bill"]["bill_id"]
        self._set_mapping("VOUCHER", tally_guid, voucher_number, "BILL", zoho_id, voucher_number, current_hash)
        self._emit_audit("SYNC", "BILL_CREATED", {
            "job_id": job.id, "tally_guid": tally_guid, "zoho_id": zoho_id, "voucher_number": voucher_number,
        })
        return {"status": "CREATED", "zoho_id": zoho_id}

    async def _sync_receipt(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        tally_guid = payload.get("tally_guid", job.source_id)
        voucher_number = payload.get("voucher_number", "")
        existing = self._get_by_guid("VOUCHER", tally_guid)
        if existing:
            return {"status": "SKIPPED", "reason": "Already synced", "zoho_id": existing["zoho_id"]}

        party_name = payload.get("party_ledger_name", "")
        customer_id = self._resolve_zoho_id_by_name("LEDGER", party_name)
        if not customer_id:
            raise ValueError(f"Customer not mapped in Zoho, sync the ledger first: {party_name!r}")

        result = await client.create_customer_payment(tm.map_customer_payment(payload, customer_id))
        zoho_id = result["customerpayment"]["customer_payment_id"]
        self._set_mapping("VOUCHER", tally_guid, voucher_number, "CUSTOMER_PAYMENT",
                          zoho_id, voucher_number, self._compute_sync_hash(payload))
        self._emit_audit("SYNC", "RECEIPT_CREATED", {
            "job_id": job.id, "tally_guid": tally_guid, "zoho_id": zoho_id,
        })
        return {"status": "CREATED", "zoho_id": zoho_id}

    async def _sync_payment(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        tally_guid = payload.get("tally_guid", job.source_id)
        voucher_number = payload.get("voucher_number", "")
        existing = self._get_by_guid("VOUCHER", tally_guid)
        if existing:
            return {"status": "SKIPPED", "reason": "Already synced", "zoho_id": existing["zoho_id"]}

        party_name = payload.get("party_ledger_name", "")
        vendor_id = self._resolve_zoho_id_by_name("LEDGER", party_name)
        if not vendor_id:
            raise ValueError(f"Vendor not mapped in Zoho, sync the ledger first: {party_name!r}")

        result = await client.create_vendor_payment(tm.map_vendor_payment(payload, vendor_id))
        zoho_id = result["vendorpayment"]["vendor_payment_id"]
        self._set_mapping("VOUCHER", tally_guid, voucher_number, "VENDOR_PAYMENT",
                          zoho_id, voucher_number, self._compute_sync_hash(payload))
        self._emit_audit("SYNC", "PAYMENT_CREATED", {
            "job_id": job.id, "tally_guid": tally_guid, "zoho_id": zoho_id,
        })
        return {"status": "CREATED", "zoho_id": zoho_id}

    async def _sync_journal(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        tally_guid = payload.get("tally_guid", job.source_id)
        voucher_number = payload.get("voucher_number", "")
        existing = self._get_by_guid("VOUCHER", tally_guid)
        if existing:
            return {"status": "SKIPPED", "reason": "Already synced", "zoho_id": existing["zoho_id"]}

        account_ids = {
            entry.get("ledger_name", ""): self._resolve_zoho_id_by_name("LEDGER", entry.get("ledger_name", ""))
            for entry in payload.get("ledger_entries", [])
        }
        account_ids = {name: zid for name, zid in account_ids.items() if zid}

        journal_data, unresolved = tm.map_journal(payload, account_ids)
        if unresolved:
            raise ValueError(f"Journal ledgers not mapped in Zoho: {', '.join(sorted(set(unresolved)))}")

        result = await client.create_journal(journal_data)
        zoho_id = result["journal"]["journal_id"]
        self._set_mapping("VOUCHER", tally_guid, voucher_number, "JOURNAL",
                          zoho_id, voucher_number, self._compute_sync_hash(payload))
        self._emit_audit("SYNC", "JOURNAL_CREATED", {
            "job_id": job.id, "tally_guid": tally_guid, "zoho_id": zoho_id,
        })
        return {"status": "CREATED", "zoho_id": zoho_id}

    async def _sync_credit_note(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        tally_guid = payload.get("tally_guid", job.source_id)
        voucher_number = payload.get("voucher_number", "")
        existing = self._get_by_guid("VOUCHER", tally_guid)
        if existing:
            return {"status": "SKIPPED", "reason": "Already synced", "zoho_id": existing["zoho_id"]}

        party_name = payload.get("party_ledger_name", "")
        customer_id = self._resolve_zoho_id_by_name("LEDGER", party_name)
        if not customer_id:
            raise ValueError(f"Customer not mapped in Zoho, sync the ledger first: {party_name!r}")

        result = await client.create_credit_note(tm.map_credit_note(payload, customer_id, self._item_ids(payload)))
        zoho_id = result["creditnote"]["creditnote_id"]
        self._set_mapping("VOUCHER", tally_guid, voucher_number, "CREDIT_NOTE",
                          zoho_id, voucher_number, self._compute_sync_hash(payload))
        return {"status": "CREATED", "zoho_id": zoho_id}

    async def _sync_debit_note(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        tally_guid = payload.get("tally_guid", job.source_id)
        voucher_number = payload.get("voucher_number", "")
        existing = self._get_by_guid("VOUCHER", tally_guid)
        if existing:
            return {"status": "SKIPPED", "reason": "Already synced", "zoho_id": existing["zoho_id"]}

        party_name = payload.get("party_ledger_name", "")
        vendor_id = self._resolve_zoho_id_by_name("LEDGER", party_name)
        if not vendor_id:
            raise ValueError(f"Vendor not mapped in Zoho, sync the ledger first: {party_name!r}")

        result = await client.create_debit_note(tm.map_debit_note(payload, vendor_id, self._item_ids(payload)))
        zoho_id = result["debitnote"]["debitnote_id"]
        self._set_mapping("VOUCHER", tally_guid, voucher_number, "DEBIT_NOTE",
                          zoho_id, voucher_number, self._compute_sync_hash(payload))
        return {"status": "CREATED", "zoho_id": zoho_id}

    def _emit_audit(self, category: str, action: str, payload: dict) -> None:
        self.db.add(AuditEvent(
            tenant_id=self.tenant_id,
            category=category,
            action=action,
            payload=payload,
        ))
        self.db.commit()


async def execute_job(db: Session, processor: "SyncProcessor", job: SyncJob) -> dict:
    """
    Run a single job through ``processor`` and drive its state machine.

    This is the single source of truth for job status transitions, shared by
    the per-job and batch endpoints.
    """
    job.status = "IN_PROGRESS"
    job.attempt += 1
    db.commit()

    try:
        result = await processor.process_job(job)
        job.status = "DONE"
        job.error_message = None
        db.commit()
        return {"job_id": job.id, "status": "success", "result": result}
    except Exception as e:
        job.error_message = str(e)
        job.status = "DEAD_LETTER" if job.attempt >= MAX_ATTEMPTS else "QUEUED"
        db.commit()
        return {"job_id": job.id, "status": "failed", "error": str(e)}


async def process_sync_job(db: Session, job_id: str) -> dict:
    """Entry point for processing a single sync job by id."""
    job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
    if not job:
        raise ValueError(f"Job not found: {job_id}")
    if job.direction != "TALLY_TO_ZOHO":
        raise ValueError(f"Only TALLY_TO_ZOHO direction supported, got: {job.direction}")

    tenant = db.query(Tenant).filter(Tenant.id == job.tenant_id).first()
    if not tenant or not tenant.zoho_org_id:
        raise ValueError("Tenant not found or missing Zoho org ID")

    zoho_client = ZohoBooksClient(db, tenant)
    processor = SyncProcessor(db, tenant.id)
    await processor.initialize(zoho_client)
    try:
        return await execute_job(db, processor, job)
    finally:
        await zoho_client.close()
