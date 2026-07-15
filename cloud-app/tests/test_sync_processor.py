"""
End-to-end SyncProcessor tests with a fake Zoho client (no network).

Proves the core Tally -> Zoho pipeline that previously produced empty records:
  - masters (contact, item) are created with populated bodies + cf_external_id,
  - the cross-reference mapping table is written,
  - an invoice resolves its customer + item ids from prior master syncs,
  - re-running an unchanged master is idempotent (SKIPPED, no second create),
  - a transaction whose party was never synced fails loudly instead of posting
    an empty document.
"""
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import SyncJob, TallyZohoMapping, Tenant
from app.sync_processor import SyncProcessor


class FakeZohoClient:
    """Records the bodies it is given and returns canned Zoho-shaped responses."""

    def __init__(self):
        self.calls = []
        self._seq = 0

    def _id(self, prefix):
        self._seq += 1
        return f"{prefix}-{self._seq}"

    async def create_contact(self, body):
        self.calls.append(("create_contact", body))
        return {"contact": {"contact_id": self._id("cust"), "contact_name": body["contact_name"]}}

    async def update_contact(self, cid, body):
        self.calls.append(("update_contact", body))
        return {"contact": {"contact_id": cid, "contact_name": body["contact_name"]}}

    async def create_item(self, body):
        self.calls.append(("create_item", body))
        return {"item": {"item_id": self._id("item"), "name": body["name"]}}

    async def update_item(self, iid, body):
        self.calls.append(("update_item", body))
        return {"item": {"item_id": iid, "name": body["name"]}}

    async def create_invoice(self, body):
        self.calls.append(("create_invoice", body))
        return {"invoice": {"invoice_id": self._id("inv")}}

    async def update_invoice(self, iid, body):
        self.calls.append(("update_invoice", body))
        return {"invoice": {"invoice_id": iid}}

    async def close(self):
        pass


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _job(tenant_id, object_type, source_id, payload):
    return SyncJob(
        tenant_id=tenant_id,
        direction="TALLY_TO_ZOHO",
        object_type=object_type,
        source_id=source_id,
        payload=payload,
    )


def test_masters_then_invoice_pipeline():
    db = _session()
    tenant = Tenant(name="Acme", zoho_org_id="org-1")
    db.add(tenant)
    db.commit()

    fake = FakeZohoClient()
    proc = SyncProcessor(db, tenant.id)
    asyncio.get_event_loop().run_until_complete(proc.initialize(fake))

    async def run():
        # 1) Sync a customer ledger.
        contact_job = _job(tenant.id, "CONTACT", "guid-acme", {
            "name": "Acme Traders", "tally_guid": "guid-acme", "is_customer": True,
            "gstin": "27AAAAA0000A1Z5", "city": "Mumbai", "state": "Maharashtra",
        })
        db.add(contact_job); db.commit()
        res = await proc.process_job(contact_job)
        assert res["status"] == "CREATED"

        # 2) Sync a stock item.
        item_job = _job(tenant.id, "ITEM", "guid-widget", {
            "name": "Widget", "tally_guid": "guid-widget", "rate": "100", "is_inventory": True,
        })
        db.add(item_job); db.commit()
        res = await proc.process_job(item_job)
        assert res["status"] == "CREATED"

        # 3) Sync an invoice that references both by name.
        inv_job = _job(tenant.id, "INVOICE", "guid-inv1", {
            "tally_guid": "guid-inv1", "voucher_number": "INV-1", "date": "20260714",
            "party_ledger_name": "Acme Traders",
            "inventory_entries": [{"stock_item_name": "Widget", "quantity": "2", "rate": "100"}],
        })
        db.add(inv_job); db.commit()
        res = await proc.process_job(inv_job)
        assert res["status"] == "CREATED"
        return

    asyncio.get_event_loop().run_until_complete(run())

    # The invoice body sent to Zoho carried a real customer_id and item_id.
    invoice_body = next(b for name, b in fake.calls if name == "create_invoice")
    assert invoice_body["customer_id"].startswith("cust-")
    assert invoice_body["line_items"][0]["item_id"].startswith("item-")
    assert invoice_body["cf_external_id"] == "guid-inv1"

    # Cross-reference rows exist for all three objects.
    mappings = db.query(TallyZohoMapping).filter(TallyZohoMapping.tenant_id == tenant.id).all()
    assert {m.tally_object_type for m in mappings} == {"LEDGER", "STOCKITEM", "VOUCHER"}


def test_unchanged_master_is_idempotent():
    db = _session()
    tenant = Tenant(name="Acme", zoho_org_id="org-1")
    db.add(tenant)
    db.commit()

    fake = FakeZohoClient()
    proc = SyncProcessor(db, tenant.id)
    asyncio.get_event_loop().run_until_complete(proc.initialize(fake))

    payload = {"name": "Acme", "tally_guid": "g1", "is_customer": True}

    async def run():
        j1 = _job(tenant.id, "CONTACT", "g1", payload)
        db.add(j1); db.commit()
        first = await proc.process_job(j1)

        j2 = _job(tenant.id, "CONTACT", "g1", dict(payload))
        db.add(j2); db.commit()
        second = await proc.process_job(j2)
        return first, second

    first, second = asyncio.get_event_loop().run_until_complete(run())
    assert first["status"] == "CREATED"
    assert second["status"] == "SKIPPED"
    # Exactly one create_contact call -- no duplicate posting.
    assert sum(1 for name, _ in fake.calls if name == "create_contact") == 1


def test_invoice_without_mapped_party_fails_loudly():
    db = _session()
    tenant = Tenant(name="Acme", zoho_org_id="org-1")
    db.add(tenant)
    db.commit()

    fake = FakeZohoClient()
    proc = SyncProcessor(db, tenant.id)
    asyncio.get_event_loop().run_until_complete(proc.initialize(fake))

    async def run():
        inv_job = _job(tenant.id, "INVOICE", "guid-inv1", {
            "tally_guid": "guid-inv1", "voucher_number": "INV-1",
            "party_ledger_name": "Never Synced",
            "inventory_entries": [],
        })
        db.add(inv_job); db.commit()
        await proc.process_job(inv_job)

    with pytest.raises(ValueError, match="Customer not mapped"):
        asyncio.get_event_loop().run_until_complete(run())

    # Nothing was posted to Zoho.
    assert not any(name == "create_invoice" for name, _ in fake.calls)
