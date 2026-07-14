"""
Tally to Zoho Books Data Mapper
Converts Tally Prime XML/JSON data to Zoho Books API format.
"""
from datetime import datetime
from typing import Any


def parse_tally_date(date_str: str | None) -> str | None:
    """Parse Tally date format (YYYYMMDD or DD-MM-YYYY) to YYYY-MM-DD."""
    if not date_str:
        return None
    # Try YYYYMMDD
    if len(date_str) == 8 and date_str.isdigit():
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    # Try DD-MM-YYYY
    try:
        return datetime.strptime(date_str, "%d-%m-%Y").strftime("%Y-%m-%d")
    except ValueError:
        pass
    # Try YYYY-MM-DD
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except ValueError:
        pass
    return None


def parse_tally_amount(amount_str: str | None) -> float:
    """Parse Tally amount string to float."""
    if not amount_str:
        return 0.0
    try:
        return float(amount_str.replace(",", ""))
    except (ValueError, AttributeError):
        return 0.0


def tally_ledger_to_zoho_contact(ledger: dict, contact_type: str = "customer") -> dict:
    """
    Convert Tally Ledger to Zoho Books Contact.
    Tally Ledger (Sundry Debtor/Creditor) -> Zoho Contact (Customer/Vendor)
    """
    mailing_details = ledger.get("MAILINGDETAILS", {})
    address_parts = []
    for key in ["ADDRESS", "ADDRESS2", "ADDRESS3"]:
        if mailing_details.get(key):
            address_parts.append(mailing_details[key])
    
    return {
        "contact_name": ledger.get("NAME", ""),
        "contact_type": contact_type,  # "customer" or "vendor"
        "contact_person": ledger.get("PARTYNAME", ""),
        "email": mailing_details.get("EMAIL", ""),
        "phone": mailing_details.get("PHONE", ""),
        "mobile": mailing_details.get("MOBILE", ""),
        "billing_address": {
            "address": " ".join(address_parts),
            "city": mailing_details.get("CITY", ""),
            "state": mailing_details.get("STATE", ""),
            "zip": mailing_details.get("PINCODE", ""),
            "country": mailing_details.get("COUNTRY", "India"),
        },
        "shipping_address": {
            "address": " ".join(address_parts),
            "city": mailing_details.get("CITY", ""),
            "state": mailing_details.get("STATE", ""),
            "zip": mailing_details.get("PINCODE", ""),
            "country": mailing_details.get("COUNTRY", "India"),
        },
        "gst_no": ledger.get("GSTIN", ""),
        "gst_treatment": ledger.get("GSTREGISTRATIONTYPE", "registered").lower(),
        "place_of_supply": ledger.get("STATENAME", ""),
        "pan": ledger.get("PAN", ""),
        "cf_external_id": ledger.get("LEDGERID", ""),  # Tally GUID
        "cf_tally_guid": ledger.get("GUID", ""),
        "cf_ledger_name": ledger.get("NAME", ""),
        "payment_terms": ledger.get("CREDITPERIOD", 0),
        "currency_code": ledger.get("CURRENCYNAME", "INR"),
        "opening_balance_amount": parse_tally_amount(ledger.get("OPENINGBALANCE", "0")),
        "opening_balance_type": "receivable" if contact_type == "customer" else "payable",
    }


def tally_stock_item_to_zoho_item(stock_item: dict) -> dict:
    """
    Convert Tally Stock Item to Zoho Books Item.
    """
    unit = stock_item.get("BASEUNITS", "Nos")
    gst_details = stock_item.get("GSTDETAILS", {})
    
    return {
        "name": stock_item.get("NAME", ""),
        "description": stock_item.get("DESCRIPTION", ""),
        "rate": parse_tally_amount(stock_item.get("STANDARDRATE", "0")),
        "unit": unit,
        "purchase_rate": parse_tally_amount(stock_item.get("PURCHASERATE", "0")),
        "tax_id": gst_details.get("TAXID", ""),  # Will map to Zoho tax
        "tax_name": gst_details.get("TAXNAME", ""),
        "tax_percentage": parse_tally_amount(gst_details.get("TAXPERCENTAGE", "0")),
        "hsn_code": gst_details.get("HSNCODE", ""),
        "sac_code": gst_details.get("SACCODE", ""),
        "product_type": "goods" if stock_item.get("ISSTOCKITEM", "Yes") == "Yes" else "service",
        "cf_external_id": stock_item.get("STOCKITEMID", ""),
        "cf_tally_guid": stock_item.get("GUID", ""),
        "cf_stock_item_name": stock_item.get("NAME", ""),
        "reorder_level": parse_tally_amount(stock_item.get("REORDERLEVEL", "0")),
        "track_inventory": stock_item.get("ISSTOCKITEM", "Yes") == "Yes",
    }


def tally_voucher_to_zoho_invoice(voucher: dict, contact_map: dict, item_map: dict) -> dict:
    """
    Convert Tally Sales Voucher to Zoho Books Invoice.
    """
    date = parse_tally_date(voucher.get("DATE", ""))
    voucher_number = voucher.get("VOUCHERNUMBER", "")
    party_ledger_name = voucher.get("PARTYLEDGERNAME", "")
    party_ledger_guid = contact_map.get(party_ledger_name, {}).get("zoho_id", "")
    
    line_items: list[dict[str, Any]] = []
    for entry in voucher.get("ALLINVENTORYENTRIES", []):
        stock_item_name = entry.get("STOCKITEMNAME", "")
        item_info = item_map.get(stock_item_name, {})
        
        qty = parse_tally_amount(entry.get("BILLEDQTY", entry.get("ACTUALQTY", "1")))
        rate = parse_tally_amount(entry.get("RATE", "0"))
        
        # Tax calculation
        tax_entries = entry.get("TAXDETAILS", [])
        tax_amount = sum(parse_tally_amount(t.get("AMOUNT", "0")) for t in tax_entries)
        
        line_items.append({
            "item_id": item_info.get("zoho_id", ""),
            "name": stock_item_name,
            "description": entry.get("NARRATION", ""),
            "item_order": len(line_items) + 1,
            "rate": rate,
            "quantity": qty,
            "unit": entry.get("UNIT", "Nos"),
            "discount": parse_tally_amount(entry.get("DISCOUNT", "0")),
            "tax_id": item_info.get("tax_id", ""),
            "tax_name": item_info.get("tax_name", ""),
            "tax_percentage": item_info.get("tax_percentage", 0),
            "tax_amount": tax_amount,
            "cf_external_id": entry.get("GUID", ""),
        })
    
    # Get narration/notes
    narration = voucher.get("NARRATION", "")
    
    return {
        "customer_id": party_ledger_guid,
        "customer_name": party_ledger_name,
        "date": date,
        "invoice_number": voucher_number,
        "reference_number": voucher.get("REFERENCE", ""),
        "place_of_supply": voucher.get("PLACEOFSUPPLY", ""),
        "gst_treatment": voucher.get("GSTTREATMENT", "business_gst").lower(),
        "gst_no": voucher.get("GSTIN", ""),
        "line_items": line_items,
        "notes": narration,
        "terms": "",
        "cf_external_id": voucher.get("GUID", ""),
        "cf_tally_voucher_number": voucher_number,
        "cf_tally_guid": voucher.get("GUID", ""),
        "payment_terms": voucher.get("CREDITPERIOD", 0),
        "payment_terms_label": f"Net {voucher.get('CREDITPERIOD', 0)}",
        "salesperson_name": voucher.get("SALESPERSON", ""),
    }


def tally_voucher_to_zoho_bill(voucher: dict, contact_map: dict, item_map: dict) -> dict:
    """
    Convert Tally Purchase Voucher to Zoho Books Bill.
    """
    date = parse_tally_date(voucher.get("DATE", ""))
    voucher_number = voucher.get("VOUCHERNUMBER", "")
    party_ledger_name = voucher.get("PARTYLEDGERNAME", "")
    party_ledger_guid = contact_map.get(party_ledger_name, {}).get("zoho_id", "")
    
    line_items: list[dict[str, Any]] = []
    for entry in voucher.get("ALLINVENTORYENTRIES", []):
        stock_item_name = entry.get("STOCKITEMNAME", "")
        item_info = item_map.get(stock_item_name, {})
        
        qty = parse_tally_amount(entry.get("BILLEDQTY", entry.get("ACTUALQTY", "1")))
        rate = parse_tally_amount(entry.get("RATE", "0"))
        
        tax_entries = entry.get("TAXDETAILS", [])
        tax_amount = sum(parse_tally_amount(t.get("AMOUNT", "0")) for t in tax_entries)
        
        line_items.append({
            "item_id": item_info.get("zoho_id", ""),
            "name": stock_item_name,
            "description": entry.get("NARRATION", ""),
            "item_order": len(line_items) + 1,
            "rate": rate,
            "quantity": qty,
            "unit": entry.get("UNIT", "Nos"),
            "tax_id": item_info.get("tax_id", ""),
            "tax_name": item_info.get("tax_name", ""),
            "tax_percentage": item_info.get("tax_percentage", 0),
            "tax_amount": tax_amount,
            "cf_external_id": entry.get("GUID", ""),
        })
    
    narration = voucher.get("NARRATION", "")
    
    return {
        "vendor_id": party_ledger_guid,
        "vendor_name": party_ledger_name,
        "date": date,
        "bill_number": voucher_number,
        "reference_number": voucher.get("REFERENCE", ""),
        "due_date": parse_tally_date(voucher.get("DUEDATE", "")),
        "place_of_supply": voucher.get("PLACEOFSUPPLY", ""),
        "gst_treatment": voucher.get("GSTTREATMENT", "business_gst").lower(),
        "gst_no": voucher.get("GSTIN", ""),
        "line_items": line_items,
        "notes": narration,
        "cf_external_id": voucher.get("GUID", ""),
        "cf_tally_voucher_number": voucher_number,
        "cf_tally_guid": voucher.get("GUID", ""),
    }


def tally_payment_to_zoho_customer_payment(voucher: dict, contact_map: dict, invoice_map: dict) -> dict:
    """
    Convert Tally Receipt Voucher to Zoho Books Customer Payment.
    """
    date = parse_tally_date(voucher.get("DATE", ""))
    voucher_number = voucher.get("VOUCHERNUMBER", "")
    party_ledger_name = voucher.get("PARTYLEDGERNAME", "")
    party_ledger_guid = contact_map.get(party_ledger_name, {}).get("zoho_id", "")
    
    # Get invoices being paid
    invoices = []
    for entry in voucher.get("LEDGERENTRIES", []):
        ledger_name = entry.get("LEDGERNAME", "")
        if ledger_name in invoice_map:
            invoices.append({
                "invoice_id": invoice_map[ledger_name]["zoho_id"],
                "invoice_number": invoice_map[ledger_name]["invoice_number"],
                "amount_applied": parse_tally_amount(entry.get("AMOUNT", "0")),
            })
    
    return {
        "customer_id": party_ledger_guid,
        "customer_name": party_ledger_name,
        "date": date,
        "payment_mode": voucher.get("PAYMENTMODE", "cash"),
        "amount": parse_tally_amount(voucher.get("AMOUNT", "0")),
        "reference_number": voucher.get("REFERENCE", ""),
        "description": voucher.get("NARRATION", ""),
        "invoices": invoices,
        "cf_external_id": voucher.get("GUID", ""),
        "cf_tally_voucher_number": voucher_number,
        "cf_tally_guid": voucher.get("GUID", ""),
    }


def tally_payment_to_zoho_vendor_payment(voucher: dict, contact_map: dict, bill_map: dict) -> dict:
    """
    Convert Tally Payment Voucher to Zoho Books Vendor Payment.
    """
    date = parse_tally_date(voucher.get("DATE", ""))
    voucher_number = voucher.get("VOUCHERNUMBER", "")
    party_ledger_name = voucher.get("PARTYLEDGERNAME", "")
    party_ledger_guid = contact_map.get(party_ledger_name, {}).get("zoho_id", "")
    
    bills = []
    for entry in voucher.get("LEDGERENTRIES", []):
        ledger_name = entry.get("LEDGERNAME", "")
        if ledger_name in bill_map:
            bills.append({
                "bill_id": bill_map[ledger_name]["zoho_id"],
                "bill_number": bill_map[ledger_name]["bill_number"],
                "amount_applied": parse_tally_amount(entry.get("AMOUNT", "0")),
            })
    
    return {
        "vendor_id": party_ledger_guid,
        "vendor_name": party_ledger_name,
        "date": date,
        "payment_mode": voucher.get("PAYMENTMODE", "cash"),
        "amount": parse_tally_amount(voucher.get("AMOUNT", "0")),
        "reference_number": voucher.get("REFERENCE", ""),
        "description": voucher.get("NARRATION", ""),
        "bills": bills,
        "cf_external_id": voucher.get("GUID", ""),
        "cf_tally_voucher_number": voucher_number,
        "cf_tally_guid": voucher.get("GUID", ""),
    }


def tally_journal_to_zoho_journal(voucher: dict, account_map: dict) -> dict:
    """
    Convert Tally Journal Voucher to Zoho Books Journal.
    """
    date = parse_tally_date(voucher.get("DATE", ""))
    voucher_number = voucher.get("VOUCHERNUMBER", "")
    
    line_items: list[dict[str, Any]] = []
    for entry in voucher.get("LEDGERENTRIES", []):
        ledger_name = entry.get("LEDGERNAME", "")
        account_info = account_map.get(ledger_name, {})
        
        amount = parse_tally_amount(entry.get("AMOUNT", "0"))
        debit_or_credit = entry.get("DEBITCREDIT", "").lower()
        
        line_items.append({
            "account_id": account_info.get("zoho_id", ""),
            "account_name": ledger_name,
            "debit_or_credit": "debit" if debit_or_credit == "debit" else "credit",
            "amount": amount,
            "description": entry.get("NARRATION", ""),
        })
    
    return {
        "date": date,
        "reference_number": voucher_number,
        "notes": voucher.get("NARRATION", ""),
        "journal_lines": line_items,
        "cf_external_id": voucher.get("GUID", ""),
        "cf_tally_voucher_number": voucher_number,
        "cf_tally_guid": voucher.get("GUID", ""),
    }


def tally_ledger_to_zoho_account(ledger: dict, group_name: str = "") -> dict:
    """
    Convert Tally Ledger to Zoho Books Chart of Account.
    """
    group_mapping = {
        "Sundry Debtors": "accounts_receivable",
        "Sundry Creditors": "accounts_payable",
        "Bank Accounts": "bank",
        "Cash": "cash",
        "Sales": "income",
        "Purchase": "expense",
        "Direct Expenses": "expense",
        "Indirect Expenses": "expense",
        "Direct Incomes": "income",
        "Indirect Incomes": "income",
        "Fixed Assets": "fixed_asset",
        "Current Assets": "current_asset",
        "Current Liabilities": "current_liability",
        "Loans (Liability)": "long_term_liability",
        "Capital": "equity",
        "Reserves & Surplus": "equity",
        "Branch / Divisions": "equity",
    }
    
    account_type = group_mapping.get(group_name, "other")
    
    return {
        "account_name": ledger.get("NAME", ""),
        "account_type": account_type,
        "description": ledger.get("NARRATION", ""),
        "group_name": group_name,
        "opening_balance": parse_tally_amount(ledger.get("OPENINGBALANCE", "0")),
        "opening_balance_type": "debit" if ledger.get("OPENINGBALANCETYPE", "").lower() == "dr" else "credit",
        "cf_external_id": ledger.get("LEDGERID", ""),
        "cf_tally_guid": ledger.get("GUID", ""),
        "cf_ledger_name": ledger.get("NAME", ""),
    }


# ============================================================
# TALLY XML PARSER (Lightweight)
# ============================================================

def parse_tally_xml(xml_string: str) -> dict:
    """
    Parse Tally XML response to Python dict.
    This is a lightweight parser for the specific TDL export format.
    """
    import xml.etree.ElementTree as ET
    
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError:
        return {}
    
    def elem_to_dict(elem: ET.Element) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for child in elem:
            if len(child) == 0:
                # Leaf node
                tag = child.tag
                text: str = child.text or ""
                if tag in result:
                    # Convert to list if multiple same tags
                    if not isinstance(result[tag], list):
                        result[tag] = [result[tag]]
                    result[tag].append(text)
                else:
                    result[tag] = text
            else:
                # Nested element
                tag = child.tag
                child_dict = elem_to_dict(child)
                if tag in result:
                    if not isinstance(result[tag], list):
                        result[tag] = [result[tag]]
                    result[tag].append(child_dict)
                else:
                    result[tag] = child_dict
        return result
    
    return elem_to_dict(root)


def parse_tally_vouchers_xml(xml_string: str) -> list[dict]:
    """Parse Tally vouchers from XML export."""
    data = parse_tally_xml(xml_string)
    vouchers = []
    
    # Tally XML structure: ENVELOPE -> BODY -> DATA -> TALLYMESSAGE -> VOUCHER
    envelope = data.get("ENVELOPE", {})
    body = envelope.get("BODY", {})
    data_section = body.get("DATA", {})
    tally_messages = data_section.get("TALLYMESSAGE", [])
    
    if not isinstance(tally_messages, list):
        tally_messages = [tally_messages]
    
    for msg in tally_messages:
        voucher = msg.get("VOUCHER", {})
        if voucher:
            vouchers.append(voucher)
    
    return vouchers


def parse_tally_masters_xml(xml_string: str) -> dict[str, list[dict]]:
    """Parse Tally masters (ledgers, stock items, groups) from XML export."""
    data = parse_tally_xml(xml_string)
    
    result: dict[str, list[dict]] = {
        "ledgers": [],
        "stock_items": [],
        "groups": [],
    }
    
    envelope = data.get("ENVELOPE", {})
    body = envelope.get("BODY", {})
    data_section = body.get("DATA", {})
    tally_messages = data_section.get("TALLYMESSAGE", [])
    
    if not isinstance(tally_messages, list):
        tally_messages = [tally_messages]
    
    for msg in tally_messages:
        if "LEDGER" in msg:
            result["ledgers"].append(msg["LEDGER"])
        elif "STOCKITEM" in msg:
            result["stock_items"].append(msg["STOCKITEM"])
        elif "GROUP" in msg:
            result["groups"].append(msg["GROUP"])
    
    return result
