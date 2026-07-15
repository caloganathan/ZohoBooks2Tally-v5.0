"""Unit tests for the canonical Tally -> Zoho Books mappers.

These lock the core fix: the agent emits normalized lowercase payloads, and the
mappers must produce populated Zoho bodies (not empty ones) with cf_external_id
stamped for idempotency.
"""
from app import tally_mapper as tm


CONTACT_PAYLOAD = {
    "name": "Acme Traders",
    "tally_guid": "guid-acme",
    "is_customer": True,
    "gstin": "27AAAAA0000A1Z5",
    "pan": "AAAAA0000A",
    "address": "12 MG Road",
    "city": "Mumbai",
    "state": "Maharashtra",
    "pincode": "400001",
    "country": "India",
    "email": "ap@acme.example",
    "phone": "022-1234",
    "mobile": "9800000000",
    "contact_person": "R. Sharma",
    "credit_period": 30,
    "currency": "INR",
}


def test_map_contact_is_populated():
    body = tm.map_contact(CONTACT_PAYLOAD, "customer")
    assert body["contact_name"] == "Acme Traders"
    assert body["contact_type"] == "customer"
    assert body["gst_no"] == "27AAAAA0000A1Z5"
    assert body["pan_no"] == "AAAAA0000A"
    assert body["gst_treatment"] == "business_gst"
    assert body["billing_address"]["city"] == "Mumbai"
    assert body["billing_address"]["zip"] == "400001"
    assert body["payment_terms"] == 30
    assert body["cf_external_id"] == "guid-acme"
    assert body["contact_persons"][0]["first_name"] == "R. Sharma"


def test_map_contact_without_gstin_is_consumer():
    body = tm.map_contact({"name": "Walk-in", "tally_guid": "g"}, "customer")
    assert body["gst_treatment"] == "consumer"
    assert "gst_no" not in body


def test_map_item_goods_vs_service():
    goods = tm.map_item({"name": "Widget", "tally_guid": "g1", "rate": "100",
                         "unit": "Nos", "hsn_code": "8481", "is_inventory": True})
    assert goods["name"] == "Widget"
    assert goods["rate"] == 100.0
    assert goods["product_type"] == "goods"
    assert goods["hsn_or_sac"] == "8481"
    assert goods["cf_external_id"] == "g1"

    service = tm.map_item({"name": "Consulting", "tally_guid": "g2", "is_inventory": False})
    assert service["product_type"] == "service"


def test_map_invoice_resolves_customer_and_items():
    payload = {
        "tally_guid": "v-1",
        "voucher_number": "INV-1",
        "date": "20260714",
        "party_ledger_name": "Acme Traders",
        "narration": "Sale",
        "inventory_entries": [
            {"stock_item_name": "Widget", "quantity": "2", "rate": "100", "unit": "Nos"},
            {"stock_item_name": "Unmapped", "quantity": "1", "rate": "50"},
        ],
    }
    body = tm.map_invoice(payload, "cust-123", {"Widget": "item-9"})
    assert body["customer_id"] == "cust-123"
    assert body["invoice_number"] == "INV-1"
    assert body["date"] == "2026-07-14"
    assert body["cf_external_id"] == "v-1"
    assert len(body["line_items"]) == 2
    assert body["line_items"][0]["item_id"] == "item-9"
    assert body["line_items"][0]["quantity"] == 2.0
    # Unmapped item still produces a name-based line rather than being dropped.
    assert "item_id" not in body["line_items"][1]
    assert body["line_items"][1]["name"] == "Unmapped"


def test_map_journal_reports_unresolved_ledgers():
    payload = {
        "tally_guid": "j-1",
        "voucher_number": "JV-1",
        "date": "20260714",
        "ledger_entries": [
            {"ledger_name": "Bank", "amount": "500", "type": "debit"},
            {"ledger_name": "Interest Income", "amount": "500", "type": "credit"},
        ],
    }
    body, unresolved = tm.map_journal(payload, {"Bank": "acc-1"})
    assert unresolved == ["Interest Income"]
    # Only the resolved line is emitted; caller must reject on unresolved.
    assert len(body["line_items"]) == 1
    assert body["line_items"][0]["account_id"] == "acc-1"
    assert body["line_items"][0]["debit_or_credit"] == "debit"

    body2, unresolved2 = tm.map_journal(payload, {"Bank": "acc-1", "Interest Income": "acc-2"})
    assert unresolved2 == []
    assert len(body2["line_items"]) == 2


def test_map_payments_carry_external_id():
    cust = tm.map_customer_payment({"tally_guid": "r-1", "amount": "250", "date": "20260714"}, "cust-1")
    assert cust["customer_id"] == "cust-1"
    assert cust["amount"] == 250.0
    assert cust["cf_external_id"] == "r-1"

    vend = tm.map_vendor_payment({"tally_guid": "p-1", "amount": "999", "date": "20260714"}, "vend-1")
    assert vend["vendor_id"] == "vend-1"
    assert vend["amount"] == 999.0
    assert vend["cf_external_id"] == "p-1"
