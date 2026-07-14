"""
Sync Processor - Handles Tally → Zoho Books synchronization logic.
Processes sync jobs from the queue and maps Tally data to Zoho Books API calls.
"""
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import AuditEvent, SyncJob, TallyZohoMapping, Tenant
from .zoho_client import ZohoBooksClient
from .tally_mapper import (
    tally_ledger_to_zoho_contact,
    tally_stock_item_to_zoho_item,
    tally_voucher_to_zoho_invoice,
    tally_voucher_to_zoho_bill,
)


class SyncProcessor:
    """Processes sync jobs for Tally → Zoho Books direction."""

    def __init__(self, db: Session, tenant_id: str):
        self.db = db
        self.tenant_id = tenant_id
        self.zoho_client: ZohoBooksClient | None = None

        # Caches for mapping lookups
        self._contact_cache: dict[str, dict] = {}  # tally_name -> {zoho_id, ...}
        self._item_cache: dict[str, dict] = {}     # tally_name -> {zoho_id, ...}
        self._account_cache: dict[str, dict] = {}  # tally_name -> {zoho_id, ...}

    async def initialize(self, zoho_client: ZohoBooksClient) -> None:
        """Initialize with Zoho client and load mapping caches."""
        self.zoho_client = zoho_client
        await self._load_mapping_caches()

    async def _load_mapping_caches(self) -> None:
        """Load existing mappings from database into memory."""
        mappings = self.db.query(TallyZohoMapping).filter(
            TallyZohoMapping.tenant_id == self.tenant_id
        ).all()

        for m in mappings:
            key = f"{m.tally_object_type}:{m.tally_object_id}"
            self._contact_cache[key] = {
                "zoho_id": m.zoho_object_id,
                "zoho_name": m.zoho_object_name,
                "zoho_type": m.zoho_object_type,
            }

    def _get_mapping(self, tally_type: str, tally_id: str) -> dict | None:
        """Get Zoho mapping for a Tally object."""
        return self._contact_cache.get(f"{tally_type}:{tally_id}")

    def _set_mapping(
        self,
        tally_type: str,
        tally_id: str,
        tally_name: str,
        zoho_type: str,
        zoho_id: str,
        zoho_name: str,
    ) -> None:
        """Store mapping in cache and database."""
        key = f"{tally_type}:{tally_id}"
        self._contact_cache[key] = {
            "zoho_id": zoho_id,
            "zoho_name": zoho_name,
            "zoho_type": zoho_type,
        }

        # Upsert in database
        mapping = self.db.query(TallyZohoMapping).filter(
            TallyZohoMapping.tenant_id == self.tenant_id,
            TallyZohoMapping.tally_object_type == tally_type,
            TallyZohoMapping.tally_object_id == tally_id,
        ).first()

        if mapping:
            mapping.zoho_object_type = zoho_type
            mapping.zoho_object_id = zoho_id
            mapping.zoho_object_name = zoho_name
            mapping.tally_object_name = tally_name
            mapping.last_synced_at = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            mapping = TallyZohoMapping(
                tenant_id=self.tenant_id,
                tally_object_type=tally_type,
                tally_object_id=tally_id,
                tally_object_name=tally_name,
                zoho_object_type=zoho_type,
                zoho_object_id=zoho_id,
                zoho_object_name=zoho_name,
            )
            self.db.add(mapping)

        self.db.commit()

    def _compute_sync_hash(self, payload: dict) -> str:
        """Compute hash for change detection."""
        # Sort keys for consistent hashing
        normalized = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(normalized.encode()).hexdigest()[:32]

    async def process_job(self, job: SyncJob) -> dict:
        """
        Process a single sync job based on object_type.

        Returns result dict with success status and details.
        """
        if self.zoho_client is None:
            raise RuntimeError("SyncProcessor not initialized. Call initialize() first.")

        client = self.zoho_client
        object_type = job.object_type
        payload = job.payload or {}
        source_id = job.source_id  # Tally GUID

        try:
            if object_type == "CONTACT":
                return await self._sync_contact(client, job, payload)
            elif object_type == "ITEM":
                return await self._sync_item(client, job, payload)
            elif object_type == "INVOICE":
                return await self._sync_invoice(client, job, payload)
            elif object_type == "BILL":
                return await self._sync_bill(client, job, payload)
            elif object_type == "PAYMENT":
                return await self._sync_payment(client, job, payload)
            elif object_type == "RECEIPT":
                return await self._sync_receipt(client, job, payload)
            elif object_type == "JOURNAL":
                return await self._sync_journal(client, job, payload)
            elif object_type == "CREDIT_NOTE":
                return await self._sync_credit_note(client, job, payload)
            elif object_type == "DEBIT_NOTE":
                return await self._sync_debit_note(client, job, payload)
            elif object_type == "LEDGER":
                return await self._sync_ledger(client, job, payload)
            else:
                raise ValueError(f"Unknown object type: {object_type}")

        except Exception as e:
            self._emit_audit("SYNC", f"JOB_FAILED_{object_type}", {
                "job_id": job.id,
                "error": str(e),
                "object_type": object_type,
                "source_id": source_id,
            })
            raise

    async def _sync_contact(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        """Sync Tally Ledger (Customer/Vendor) to Zoho Contact."""
        tally_guid = payload.get("tally_guid", job.source_id)
        ledger_name = payload.get("name", "")
        contact_type = "customer" if payload.get("is_customer", True) else "vendor"

        # Check if already synced (idempotency)
        existing = self._get_mapping("LEDGER", tally_guid)
        if existing:
            # Check if changed
            current_hash = self._compute_sync_hash(payload)
            mapping = self.db.query(TallyZohoMapping).filter(
                TallyZohoMapping.tenant_id == self.tenant_id,
                TallyZohoMapping.tally_object_type == "LEDGER",
                TallyZohoMapping.tally_object_id == tally_guid,
            ).first()

            if mapping and mapping.sync_hash == current_hash:
                return {"status": "SKIPPED", "reason": "No changes", "zoho_id": existing["zoho_id"]}

            # Update existing
            zoho_data = tally_ledger_to_zoho_contact(payload, contact_type)
            result = await client.update_contact(existing["zoho_id"], zoho_data)
            self._set_mapping("LEDGER", tally_guid, ledger_name, "CONTACT", result["contact"]["contact_id"], result["contact"]["contact_name"])
            mapping.sync_hash = current_hash
            self.db.commit()
            return {"status": "UPDATED", "zoho_id": result["contact"]["contact_id"]}

        # Create new
        zoho_data = tally_ledger_to_zoho_contact(payload, contact_type)
        result = await client.create_contact(zoho_data)
        zoho_id = result["contact"]["contact_id"]
        zoho_name = result["contact"]["contact_name"]

        self._set_mapping("LEDGER", tally_guid, ledger_name, "CONTACT", zoho_id, zoho_name)

        self._emit_audit("SYNC", "CONTACT_CREATED", {
            "job_id": job.id,
            "tally_guid": tally_guid,
            "zoho_id": zoho_id,
            "contact_type": contact_type,
        })

        return {"status": "CREATED", "zoho_id": zoho_id}

    async def _sync_item(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        """Sync Tally Stock Item to Zoho Item."""
        tally_guid = payload.get("tally_guid", job.source_id)
        item_name = payload.get("name", "")

        existing = self._get_mapping("STOCKITEM", tally_guid)
        if existing:
            current_hash = self._compute_sync_hash(payload)
            mapping = self.db.query(TallyZohoMapping).filter(
                TallyZohoMapping.tenant_id == self.tenant_id,
                TallyZohoMapping.tally_object_type == "STOCKITEM",
                TallyZohoMapping.tally_object_id == tally_guid,
            ).first()

            if mapping and mapping.sync_hash == current_hash:
                return {"status": "SKIPPED", "reason": "No changes", "zoho_id": existing["zoho_id"]}

            zoho_data = tally_stock_item_to_zoho_item(payload)
            result = await client.update_item(existing["zoho_id"], zoho_data)
            self._set_mapping("STOCKITEM", tally_guid, item_name, "ITEM", result["item"]["item_id"], result["item"]["name"])
            mapping.sync_hash = current_hash
            self.db.commit()
            return {"status": "UPDATED", "zoho_id": result["item"]["item_id"]}

        zoho_data = tally_stock_item_to_zoho_item(payload)
        result = await client.create_item(zoho_data)
        zoho_id = result["item"]["item_id"]
        zoho_name = result["item"]["name"]

        self._set_mapping("STOCKITEM", tally_guid, item_name, "ITEM", zoho_id, zoho_name)

        self._emit_audit("SYNC", "ITEM_CREATED", {
            "job_id": job.id,
            "tally_guid": tally_guid,
            "zoho_id": zoho_id,
        })

        return {"status": "CREATED", "zoho_id": zoho_id}

    async def _sync_invoice(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        """Sync Tally Sales Voucher to Zoho Invoice."""
        tally_guid = payload.get("tally_guid", job.source_id)
        voucher_number = payload.get("voucher_number", "")

        # Resolve dependencies: contacts and items
        contact_map = await self._resolve_contacts(payload)
        item_map = await self._resolve_items(payload)

        existing = self._get_mapping("VOUCHER", tally_guid)
        if existing:
            current_hash = self._compute_sync_hash(payload)
            mapping = self.db.query(TallyZohoMapping).filter(
                TallyZohoMapping.tenant_id == self.tenant_id,
                TallyZohoMapping.tally_object_type == "VOUCHER",
                TallyZohoMapping.tally_object_id == tally_guid,
            ).first()

            if mapping and mapping.sync_hash == current_hash:
                return {"status": "SKIPPED", "reason": "No changes", "zoho_id": existing["zoho_id"]}

            zoho_data = tally_voucher_to_zoho_invoice(payload, contact_map, item_map)
            result = await client.update_invoice(existing["zoho_id"], zoho_data)
            self._set_mapping("VOUCHER", tally_guid, voucher_number, "INVOICE", result["invoice"]["invoice_id"], voucher_number)
            mapping.sync_hash = current_hash
            self.db.commit()
            return {"status": "UPDATED", "zoho_id": result["invoice"]["invoice_id"]}

        zoho_data = tally_voucher_to_zoho_invoice(payload, contact_map, item_map)
        result = await client.create_invoice(zoho_data)
        zoho_id = result["invoice"]["invoice_id"]

        self._set_mapping("VOUCHER", tally_guid, voucher_number, "INVOICE", zoho_id, voucher_number)

        self._emit_audit("SYNC", "INVOICE_CREATED", {
            "job_id": job.id,
            "tally_guid": tally_guid,
            "zoho_id": zoho_id,
            "voucher_number": voucher_number,
        })

        return {"status": "CREATED", "zoho_id": zoho_id}

    async def _sync_bill(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        """Sync Tally Purchase Voucher to Zoho Bill."""

        tally_guid = payload.get("tally_guid", job.source_id)
        voucher_number = payload.get("voucher_number", "")

        contact_map = await self._resolve_contacts(payload)
        item_map = await self._resolve_items(payload)

        existing = self._get_mapping("VOUCHER", tally_guid)
        if existing:
            current_hash = self._compute_sync_hash(payload)
            mapping = self.db.query(TallyZohoMapping).filter(
                TallyZohoMapping.tenant_id == self.tenant_id,
                TallyZohoMapping.tally_object_type == "VOUCHER",
                TallyZohoMapping.tally_object_id == tally_guid,
            ).first()

            if mapping and mapping.sync_hash == current_hash:
                return {"status": "SKIPPED", "reason": "No changes", "zoho_id": existing["zoho_id"]}

            zoho_data = tally_voucher_to_zoho_bill(payload, contact_map, item_map)
            result = await client.update_bill(existing["zoho_id"], zoho_data)
            self._set_mapping("VOUCHER", tally_guid, voucher_number, "BILL", result["bill"]["bill_id"], voucher_number)
            mapping.sync_hash = current_hash
            self.db.commit()
            return {"status": "UPDATED", "zoho_id": result["bill"]["bill_id"]}

        zoho_data = tally_voucher_to_zoho_bill(payload, contact_map, item_map)
        result = await client.create_bill(zoho_data)
        zoho_id = result["bill"]["bill_id"]

        self._set_mapping("VOUCHER", tally_guid, voucher_number, "BILL", zoho_id, voucher_number)

        self._emit_audit("SYNC", "BILL_CREATED", {
            "job_id": job.id,
            "tally_guid": tally_guid,
            "zoho_id": zoho_id,
            "voucher_number": voucher_number,
        })

        return {"status": "CREATED", "zoho_id": zoho_id}

    async def _sync_payment(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        """Sync Tally Payment Voucher to Zoho Vendor Payment."""
        tally_guid = payload.get("tally_guid", job.source_id)
        voucher_number = payload.get("voucher_number", "")

        existing = self._get_mapping("VOUCHER", tally_guid)
        if existing:
            return {"status": "SKIPPED", "reason": "Already synced", "zoho_id": existing["zoho_id"]}

        # Resolve vendor contact
        party_name = payload.get("party_ledger_name", "")
        contact_map = await self._resolve_contacts(payload)
        vendor_id = contact_map.get(party_name, {}).get("zoho_id")

        if not vendor_id:
            raise ValueError(f"Vendor not found in Zoho: {party_name}")

        payment_data = {
            "vendor_id": vendor_id,
            "date": payload.get("date", ""),
            "amount": float(payload.get("amount", 0)),
            "payment_mode": payload.get("payment_mode", "Cash"),
            "reference_number": payload.get("reference", voucher_number),
            "description": payload.get("narration", ""),
            "cf_external_id": tally_guid,
        }

        result = await client.create_vendor_payment(payment_data)
        zoho_id = result["vendorpayment"]["vendor_payment_id"]

        self._set_mapping("VOUCHER", tally_guid, voucher_number, "VENDOR_PAYMENT", zoho_id, voucher_number)

        self._emit_audit("SYNC", "PAYMENT_CREATED", {
            "job_id": job.id,
            "tally_guid": tally_guid,
            "zoho_id": zoho_id,
        })

        return {"status": "CREATED", "zoho_id": zoho_id}

    async def _sync_receipt(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        """Sync Tally Receipt Voucher to Zoho Customer Payment."""
        tally_guid = payload.get("tally_guid", job.source_id)
        voucher_number = payload.get("voucher_number", "")

        existing = self._get_mapping("VOUCHER", tally_guid)
        if existing:
            return {"status": "SKIPPED", "reason": "Already synced", "zoho_id": existing["zoho_id"]}

        party_name = payload.get("party_ledger_name", "")
        contact_map = await self._resolve_contacts(payload)
        customer_id = contact_map.get(party_name, {}).get("zoho_id")

        if not customer_id:
            raise ValueError(f"Customer not found in Zoho: {party_name}")

        payment_data = {
            "customer_id": customer_id,
            "date": payload.get("date", ""),
            "amount": float(payload.get("amount", 0)),
            "payment_mode": payload.get("payment_mode", "Cash"),
            "reference_number": payload.get("reference", voucher_number),
            "description": payload.get("narration", ""),
            "cf_external_id": tally_guid,
        }

        result = await client.create_customer_payment(payment_data)
        zoho_id = result["customerpayment"]["customer_payment_id"]

        self._set_mapping("VOUCHER", tally_guid, voucher_number, "CUSTOMER_PAYMENT", zoho_id, voucher_number)

        self._emit_audit("SYNC", "RECEIPT_CREATED", {
            "job_id": job.id,
            "tally_guid": tally_guid,
            "zoho_id": zoho_id,
        })

        return {"status": "CREATED", "zoho_id": zoho_id}

    async def _sync_journal(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        """Sync Tally Journal Voucher to Zoho Journal."""
        tally_guid = payload.get("tally_guid", job.source_id)
        voucher_number = payload.get("voucher_number", "")

        existing = self._get_mapping("VOUCHER", tally_guid)
        if existing:
            return {"status": "SKIPPED", "reason": "Already synced", "zoho_id": existing["zoho_id"]}

        # Build journal lines from payload
        lines = []
        for entry in payload.get("ledger_entries", []):
            ledger_name = entry.get("ledger_name", "")
            account = self._get_mapping("LEDGER", entry.get("ledger_guid", ""))
            if not account:
                # Try to find by name
                for key, val in self._contact_cache.items():
                    if key.startswith("LEDGER:") and val.get("zoho_name") == ledger_name:
                        account = val
                        break

            if not account:
                raise ValueError(f"Ledger not mapped: {ledger_name}")

            lines.append({
                "account_id": account["zoho_id"],
                "debit_or_credit": entry.get("type", "debit").lower(),
                "amount": float(entry.get("amount", 0)),
            })

        journal_data = {
            "date": payload.get("date", ""),
            "reference_number": voucher_number,
            "notes": payload.get("narration", ""),
            "line_items": lines,
            "cf_external_id": tally_guid,
        }

        result = await client.create_journal(journal_data)
        zoho_id = result["journal"]["journal_id"]

        self._set_mapping("VOUCHER", tally_guid, voucher_number, "JOURNAL", zoho_id, voucher_number)

        self._emit_audit("SYNC", "JOURNAL_CREATED", {
            "job_id": job.id,
            "tally_guid": tally_guid,
            "zoho_id": zoho_id,
        })

        return {"status": "CREATED", "zoho_id": zoho_id}

    async def _sync_credit_note(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        """Sync Tally Credit Note to Zoho Credit Note."""
        # Similar to invoice but for credit notes
        tally_guid = payload.get("tally_guid", job.source_id)
        existing = self._get_mapping("VOUCHER", tally_guid)
        if existing:
            return {"status": "SKIPPED", "reason": "Already synced", "zoho_id": existing["zoho_id"]}

        contact_map = await self._resolve_contacts(payload)
        item_map = await self._resolve_items(payload)

        from .tally_mapper import tally_voucher_to_zoho_invoice
        # Convert to credit note format
        invoice_data = tally_voucher_to_zoho_invoice(payload, contact_map, item_map)

        # Use credit note API
        result = await client.create_credit_note(invoice_data)
        zoho_id = result["creditnote"]["creditnote_id"]

        self._set_mapping("VOUCHER", tally_guid, payload.get("voucher_number", ""), "CREDIT_NOTE", zoho_id, "")

        return {"status": "CREATED", "zoho_id": zoho_id}

    async def _sync_debit_note(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        """Sync Tally Debit Note to Zoho Debit Note (Vendor Credit)."""
        tally_guid = payload.get("tally_guid", job.source_id)
        existing = self._get_mapping("VOUCHER", tally_guid)
        if existing:
            return {"status": "SKIPPED", "reason": "Already synced", "zoho_id": existing["zoho_id"]}

        contact_map = await self._resolve_contacts(payload)
        item_map = await self._resolve_items(payload)

        bill_data = tally_voucher_to_zoho_bill(payload, contact_map, item_map)

        result = await client.create_debit_note(bill_data)
        zoho_id = result["debitnote"]["debitnote_id"]

        self._set_mapping("VOUCHER", tally_guid, payload.get("voucher_number", ""), "DEBIT_NOTE", zoho_id, "")

        return {"status": "CREATED", "zoho_id": zoho_id}

    async def _sync_ledger(self, client: ZohoBooksClient, job: SyncJob, payload: dict) -> dict:
        """Sync Tally Ledger (Chart of Accounts) to Zoho Account."""
        tally_guid = payload.get("tally_guid", job.source_id)
        ledger_name = payload.get("name", "")

        existing = self._get_mapping("LEDGER", tally_guid)
        if existing:
            return {"status": "SKIPPED", "reason": "Already synced", "zoho_id": existing["zoho_id"]}

        account_data = {
            "account_name": ledger_name,
            "account_type": payload.get("account_type", "other"),
            "parent_account_id": payload.get("parent_account_zoho_id", ""),
            "description": payload.get("description", ""),
            "cf_external_id": tally_guid,
        }

        result = await client.create_account(account_data)
        zoho_id = result["chartofaccount"]["account_id"]

        self._set_mapping("LEDGER", tally_guid, ledger_name, "ACCOUNT", zoho_id, ledger_name)

        return {"status": "CREATED", "zoho_id": zoho_id}

    async def _resolve_contacts(self, payload: dict) -> dict:
        """Resolve Tally party ledger names to Zoho contact IDs."""
        contact_map = {}

        # Get party ledger name from payload
        party_name = payload.get("party_ledger_name") or payload.get("name", "")
        if party_name:
            mapping = self._get_mapping("LEDGER", party_name)
            if mapping:
                contact_map[party_name] = mapping
            else:
                # Try to find by name in cache
                for key, val in self._contact_cache.items():
                    if key.startswith("LEDGER:") and val.get("zoho_name") == party_name:
                        contact_map[party_name] = val
                        break

        return contact_map

    async def _resolve_items(self, payload: dict) -> dict:
        """Resolve Tally stock item names to Zoho item IDs."""
        item_map = {}

        for entry in payload.get("inventory_entries", []):
            item_name = entry.get("stock_item_name", "")
            if item_name:
                # Search in cache
                for key, val in self._contact_cache.items():
                    if key.startswith("STOCKITEM:") and val.get("zoho_name") == item_name:
                        item_map[item_name] = val
                        break

        return item_map

    def _emit_audit(self, category: str, action: str, payload: dict) -> None:
        """Emit audit event."""
        self.db.add(AuditEvent(
            tenant_id=self.tenant_id,
            category=category,
            action=action,
            payload=payload,
        ))
        self.db.commit()


async def process_sync_job(db: Session, job_id: str) -> dict:
    """Main entry point for processing a sync job."""
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

    # Update job status
    job.status = "IN_PROGRESS"
    job.attempt += 1
    db.commit()

    try:
        result = await processor.process_job(job)

        job.status = "DONE"
        job.error_message = None
        db.commit()

        return {"job_id": job_id, "status": "success", "result": result}

    except Exception as e:
        job.error_message = str(e)
        if job.attempt >= 5:
            job.status = "DEAD_LETTER"
        else:
            job.status = "QUEUED"
        db.commit()

        return {"job_id": job_id, "status": "failed", "error": str(e)}
