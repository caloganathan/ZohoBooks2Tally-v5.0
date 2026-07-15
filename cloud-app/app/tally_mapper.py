"""
Canonical Tally -> Zoho Books mappers.

These functions consume the *normalized* payload emitted by the on-prem agent
(lowercase keys, see ``agent/app/tally_http.py::tally_voucher_to_sync_payload``
and the master payloads built in ``agent/app/main.py``) and produce Zoho Books
India-edition API request bodies.

Every mapper stamps ``cf_external_id`` with the Tally GUID so the cross-reference
survives even if the app-owned mapping table is lost, and so re-syncs are
idempotent rather than duplicating financial documents.

Canonical payload shapes
-------------------------
CONTACT / LEDGER (customer, vendor or account):
    name, tally_guid, is_customer(bool), contact_type, gstin, pan,
    address, city, state, pincode, country, email, phone, mobile,
    contact_person, opening_balance, credit_period, currency, account_type

ITEM (stock item / service):
    name, tally_guid, description, unit, rate, purchase_rate, tax_id,
    tax_name, tax_percentage, hsn_code, sac_code, is_inventory(bool),
    reorder_level, sku

VOUCHER (invoice, bill, credit/debit note):
    tally_guid, voucher_number, date, due_date, party_ledger_name, reference,
    narration, place_of_supply, gst_treatment, gstin, amount,
    inventory_entries:[{stock_item_name, quantity, rate, amount, unit, discount, guid}],
    ledger_entries:[{ledger_name, amount, type, narration}]
"""
from datetime import datetime
from typing import Any


def parse_tally_date(date_str: str | None) -> str | None:
    """Parse a Tally date (YYYYMMDD, DD-MM-YYYY or YYYY-MM-DD) to YYYY-MM-DD."""
    if not date_str:
        return None
    if len(date_str) == 8 and date_str.isdigit():
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _f(value: Any, default: float = 0.0) -> float:
    """Coerce a possibly-string numeric value to float."""
    if value is None or value == "":
        return default
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return default


def _gst_treatment(payload: dict) -> str:
    """Return a valid Zoho India gst_treatment enum value."""
    explicit = (payload.get("gst_treatment") or "").strip().lower()
    valid = {
        "business_gst", "business_none", "consumer",
        "overseas", "sez", "sez_developer", "deemed_export",
    }
    if explicit in valid:
        return explicit
    return "business_gst" if payload.get("gstin") else "consumer"


def _address_block(payload: dict) -> dict:
    return {
        "address": payload.get("address", ""),
        "city": payload.get("city", ""),
        "state": payload.get("state", ""),
        "zip": payload.get("pincode", ""),
        "country": payload.get("country", "India"),
    }


def map_contact(payload: dict, contact_type: str) -> dict:
    """Map a Tally ledger (Sundry Debtor/Creditor) to a Zoho Books contact."""
    address = _address_block(payload)
    contact_person = payload.get("contact_person", "")
    email = payload.get("email", "")

    body: dict[str, Any] = {
        "contact_name": payload.get("name", ""),
        "contact_type": contact_type,  # "customer" or "vendor"
        "currency_code": payload.get("currency", "INR"),
        "payment_terms": int(_f(payload.get("credit_period", 0))),
        "gst_treatment": _gst_treatment(payload),
        "billing_address": address,
        "shipping_address": dict(address),
        "cf_external_id": payload.get("tally_guid", ""),
    }
    if payload.get("gstin"):
        body["gst_no"] = payload["gstin"]
    if payload.get("pan"):
        body["pan_no"] = payload["pan"]
    if payload.get("state"):
        body["place_of_supply"] = payload["state"]
    if contact_person or email:
        body["contact_persons"] = [{
            "first_name": contact_person,
            "email": email,
            "phone": payload.get("phone", ""),
            "mobile": payload.get("mobile", ""),
            "is_primary_contact": True,
        }]
    return body


def map_item(payload: dict) -> dict:
    """Map a Tally stock item / service to a Zoho Books item."""
    is_inventory = bool(payload.get("is_inventory", True))
    body: dict[str, Any] = {
        "name": payload.get("name", ""),
        "description": payload.get("description", ""),
        "rate": _f(payload.get("rate", 0)),
        "unit": payload.get("unit", "Nos"),
        "product_type": "goods" if is_inventory else "service",
        "item_type": "sales_and_purchases",
        "cf_external_id": payload.get("tally_guid", ""),
    }
    if payload.get("purchase_rate"):
        body["purchase_rate"] = _f(payload["purchase_rate"])
    hsn_or_sac = payload.get("hsn_code") or payload.get("sac_code")
    if hsn_or_sac:
        body["hsn_or_sac"] = hsn_or_sac
    if payload.get("sku"):
        body["sku"] = payload["sku"]
    if payload.get("tax_id"):
        body["tax_id"] = payload["tax_id"]
    return body


def map_account(payload: dict) -> dict:
    """Map a Tally ledger to a Zoho Books chart-of-accounts entry."""
    body: dict[str, Any] = {
        "account_name": payload.get("name", ""),
        "account_type": payload.get("account_type", "other_current_liability"),
        "description": payload.get("description", ""),
        "cf_external_id": payload.get("tally_guid", ""),
    }
    if payload.get("parent_account_zoho_id"):
        body["parent_account_id"] = payload["parent_account_zoho_id"]
    return body


def _line_items(payload: dict, item_id_by_name: dict[str, str]) -> list[dict]:
    lines: list[dict[str, Any]] = []
    for entry in payload.get("inventory_entries", []):
        name = entry.get("stock_item_name", "")
        line: dict[str, Any] = {
            "name": name,
            "rate": _f(entry.get("rate", 0)),
            "quantity": _f(entry.get("quantity", 1), 1.0),
            "unit": entry.get("unit", "") or "Nos",
        }
        if entry.get("discount"):
            line["discount"] = _f(entry.get("discount", 0))
        item_id = item_id_by_name.get(name)
        if item_id:
            line["item_id"] = item_id
        lines.append(line)
    return lines


def map_invoice(payload: dict, customer_id: str | None, item_id_by_name: dict[str, str]) -> dict:
    """Map a Tally sales voucher to a Zoho Books invoice."""
    body: dict[str, Any] = {
        "date": parse_tally_date(payload.get("date")) or payload.get("date", ""),
        "invoice_number": payload.get("voucher_number", ""),
        "reference_number": payload.get("reference", ""),
        "line_items": _line_items(payload, item_id_by_name),
        "notes": payload.get("narration", ""),
        "cf_external_id": payload.get("tally_guid", ""),
    }
    if customer_id:
        body["customer_id"] = customer_id
    if payload.get("due_date"):
        body["due_date"] = parse_tally_date(payload.get("due_date"))
    if payload.get("place_of_supply"):
        body["place_of_supply"] = payload["place_of_supply"]
    return body


def map_bill(payload: dict, vendor_id: str | None, item_id_by_name: dict[str, str]) -> dict:
    """Map a Tally purchase voucher to a Zoho Books bill."""
    body: dict[str, Any] = {
        "date": parse_tally_date(payload.get("date")) or payload.get("date", ""),
        "bill_number": payload.get("voucher_number", ""),
        "reference_number": payload.get("reference", ""),
        "line_items": _line_items(payload, item_id_by_name),
        "notes": payload.get("narration", ""),
        "cf_external_id": payload.get("tally_guid", ""),
    }
    if vendor_id:
        body["vendor_id"] = vendor_id
    if payload.get("due_date"):
        body["due_date"] = parse_tally_date(payload.get("due_date"))
    return body


def map_credit_note(payload: dict, customer_id: str | None, item_id_by_name: dict[str, str]) -> dict:
    """Map a Tally credit note to a Zoho Books credit note."""
    body: dict[str, Any] = {
        "date": parse_tally_date(payload.get("date")) or payload.get("date", ""),
        "creditnote_number": payload.get("voucher_number", ""),
        "reference_number": payload.get("reference", ""),
        "line_items": _line_items(payload, item_id_by_name),
        "notes": payload.get("narration", ""),
        "cf_external_id": payload.get("tally_guid", ""),
    }
    if customer_id:
        body["customer_id"] = customer_id
    return body


def map_debit_note(payload: dict, vendor_id: str | None, item_id_by_name: dict[str, str]) -> dict:
    """Map a Tally debit note to a Zoho Books vendor credit / debit note."""
    body: dict[str, Any] = {
        "date": parse_tally_date(payload.get("date")) or payload.get("date", ""),
        "debit_note_number": payload.get("voucher_number", ""),
        "reference_number": payload.get("reference", ""),
        "line_items": _line_items(payload, item_id_by_name),
        "notes": payload.get("narration", ""),
        "cf_external_id": payload.get("tally_guid", ""),
    }
    if vendor_id:
        body["vendor_id"] = vendor_id
    return body


def map_journal(payload: dict, account_id_by_name: dict[str, str]) -> tuple[dict, list[str]]:
    """
    Map a Tally journal voucher to a Zoho Books journal.

    Returns ``(body, unresolved_ledger_names)``. The caller should treat a
    non-empty ``unresolved_ledger_names`` as a hard error (a journal line
    without a mapped account cannot post correctly and must not be silently
    dropped).
    """
    lines: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for entry in payload.get("ledger_entries", []):
        ledger_name = entry.get("ledger_name", "")
        account_id = account_id_by_name.get(ledger_name)
        if not account_id:
            unresolved.append(ledger_name)
            continue
        dc = (entry.get("type", "") or "").strip().lower()
        lines.append({
            "account_id": account_id,
            "debit_or_credit": "debit" if dc == "debit" else "credit",
            "amount": _f(entry.get("amount", 0)),
            "description": entry.get("narration", ""),
        })

    body = {
        "journal_date": parse_tally_date(payload.get("date")) or payload.get("date", ""),
        "reference_number": payload.get("voucher_number", ""),
        "notes": payload.get("narration", ""),
        "line_items": lines,
        "cf_external_id": payload.get("tally_guid", ""),
    }
    return body, unresolved


def map_customer_payment(payload: dict, customer_id: str) -> dict:
    """Map a Tally receipt voucher to a Zoho Books customer payment."""
    return {
        "customer_id": customer_id,
        "date": parse_tally_date(payload.get("date")) or payload.get("date", ""),
        "amount": _f(payload.get("amount", 0)),
        "payment_mode": payload.get("payment_mode", "cash"),
        "reference_number": payload.get("reference") or payload.get("voucher_number", ""),
        "description": payload.get("narration", ""),
        "cf_external_id": payload.get("tally_guid", ""),
    }


def map_vendor_payment(payload: dict, vendor_id: str) -> dict:
    """Map a Tally payment voucher to a Zoho Books vendor payment."""
    return {
        "vendor_id": vendor_id,
        "date": parse_tally_date(payload.get("date")) or payload.get("date", ""),
        "amount": _f(payload.get("amount", 0)),
        "payment_mode": payload.get("payment_mode", "cash"),
        "reference_number": payload.get("reference") or payload.get("voucher_number", ""),
        "description": payload.get("narration", ""),
        "cf_external_id": payload.get("tally_guid", ""),
    }
