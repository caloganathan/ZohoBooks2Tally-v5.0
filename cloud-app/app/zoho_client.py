import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from .config import settings
from .models import Tenant, ZohoToken


@dataclass
class ZohoTokens:
    access_token: str
    refresh_token: str
    expires_at: int  # Unix timestamp
    scope: str


class ZohoBooksClient:
    """Zoho Books India Edition API Client with OAuth 2.0 support."""
    
    API_BASE = "https://www.zohoapis.in/books/v3"
    TOKEN_URL = "https://accounts.zoho.in/oauth/v2/token"
    
    def __init__(self, db: Session, tenant: Tenant):
        self.db = db
        self.tenant = tenant
        org_id = tenant.zoho_org_id or settings.zoho_books_org_id
        if not org_id:
            raise ValueError("Zoho Books organization ID is required")
        self.org_id: str = org_id
        self._tokens: ZohoTokens | None = None
        self._client = httpx.AsyncClient(timeout=30.0)
    
    async def _get_valid_tokens(self) -> ZohoTokens:
        """Get valid access token, refreshing if needed."""
        if self._tokens and self._tokens.expires_at > time.time() + 60:
            return self._tokens
        
        # Load from DB
        token_record = self.db.query(ZohoToken).filter(
            ZohoToken.tenant_id == self.tenant.id
        ).first()
        
        if token_record:
            self._tokens = ZohoTokens(
                access_token=token_record.access_token,
                refresh_token=token_record.refresh_token,
                expires_at=token_record.expires_at,
                scope=token_record.scope or "",
            )
            
            if self._tokens.expires_at > time.time() + 60:
                return self._tokens
            
            # Refresh token
            await self._refresh_token(token_record)
            return self._tokens
        
        raise ValueError("No Zoho tokens found for tenant. Run OAuth flow first.")
    
    async def _refresh_token(self, token_record: ZohoToken) -> None:
        """Refresh the access token using refresh token."""
        data = {
            "refresh_token": token_record.refresh_token,
            "client_id": settings.zoho_client_id,
            "client_secret": settings.zoho_client_secret,
            "grant_type": "refresh_token",
        }
        
        resp = await self._client.post(self.TOKEN_URL, data=data)
        resp.raise_for_status()
        token_data = resp.json()
        
        expires_at = int(time.time()) + token_data.get("expires_in", 3600)
        
        # Update DB
        token_record.access_token = token_data["access_token"]
        token_record.expires_at = expires_at
        if "refresh_token" in token_data:
            token_record.refresh_token = token_data["refresh_token"]
        token_record.scope = token_data.get("scope", "")
        token_record.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.db.commit()
        
        self._tokens = ZohoTokens(
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token", token_record.refresh_token),
            expires_at=expires_at,
            scope=token_data.get("scope", ""),
        )
    
    def _auth_headers(self, tokens: ZohoTokens) -> dict:
        return {
            "Authorization": f"Zoho-oauthtoken {tokens.access_token}",
            "Content-Type": "application/json",
        }
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json_data: dict | None = None,
    ) -> dict:
        """Make authenticated request to Zoho Books API."""
        tokens = await self._get_valid_tokens()
        
        url = f"{self.API_BASE}/{endpoint.lstrip('/')}"
        if params is None:
            params = {}
        params["organization_id"] = self.org_id
        
        headers = self._auth_headers(tokens)
        
        resp = await self._client.request(
            method, url, params=params, json=json_data, headers=headers
        )
        
        if resp.status_code == 401:
            # Force token refresh and retry once
            token_record = self.db.query(ZohoToken).filter(
                ZohoToken.tenant_id == self.tenant.id
            ).first()
            if token_record:
                await self._refresh_token(token_record)
                tokens = await self._get_valid_tokens()
                headers = self._auth_headers(tokens)
                resp = await self._client.request(
                    method, url, params=params, json=json_data, headers=headers
                )
        
        resp.raise_for_status()
        return resp.json()
    
    # ============ CONTACTS (Customers/Vendors) ============
    
    async def create_contact(self, contact_data: dict) -> dict:
        """Create a contact (customer or vendor) in Zoho Books."""
        return await self._request("POST", "contacts", json_data=contact_data)
    
    async def get_contact(self, contact_id: str) -> dict:
        """Get a contact by ID."""
        return await self._request("GET", f"contacts/{contact_id}")
    
    async def update_contact(self, contact_id: str, contact_data: dict) -> dict:
        """Update a contact."""
        return await self._request("PUT", f"contacts/{contact_id}", json_data=contact_data)
    
    async def list_contacts(
        self,
        contact_type: str | None = None,
        page: int = 1,
        per_page: int = 200,
    ) -> dict:
        """List contacts with pagination."""
        params: dict[str, str | int] = {"page": page, "per_page": per_page}
        if contact_type:
            params["contact_type"] = contact_type
        return await self._request("GET", "contacts", params=params)
    
    async def search_contact_by_external_id(self, external_id: str) -> dict | None:
        """Search contact by custom field (external_id from Tally)."""
        # Zoho Books doesn't have direct external_id search, use custom field search
        # This requires a custom field to be set up in Zoho
        resp = await self.list_contacts(per_page=200)
        for contact in resp.get("contacts", []):
            if contact.get("cf_external_id") == external_id:
                return contact
        return None
    
    # ============ ITEMS ============
    
    async def create_item(self, item_data: dict) -> dict:
        """Create an item in Zoho Books."""
        return await self._request("POST", "items", json_data=item_data)
    
    async def get_item(self, item_id: str) -> dict:
        """Get an item by ID."""
        return await self._request("GET", f"items/{item_id}")
    
    async def update_item(self, item_id: str, item_data: dict) -> dict:
        """Update an item."""
        return await self._request("PUT", f"items/{item_id}", json_data=item_data)
    
    async def list_items(self, page: int = 1, per_page: int = 200) -> dict:
        """List items."""
        return await self._request("GET", "items", params={"page": page, "per_page": per_page})
    
    async def search_item_by_external_id(self, external_id: str) -> dict | None:
        """Search item by external ID (Tally stock item ID)."""
        resp = await self.list_items(per_page=200)
        for item in resp.get("items", []):
            if item.get("cf_external_id") == external_id:
                return item
        return None
    
    # ============ INVOICES ============
    
    async def create_invoice(self, invoice_data: dict) -> dict:
        """Create a sales invoice."""
        return await self._request("POST", "invoices", json_data=invoice_data)
    
    async def get_invoice(self, invoice_id: str) -> dict:
        """Get an invoice by ID."""
        return await self._request("GET", f"invoices/{invoice_id}")
    
    async def update_invoice(self, invoice_id: str, invoice_data: dict) -> dict:
        """Update an invoice."""
        return await self._request("PUT", f"invoices/{invoice_id}", json_data=invoice_data)
    
    async def void_invoice(self, invoice_id: str) -> dict:
        """Void an invoice."""
        return await self._request("POST", f"invoices/{invoice_id}/status/void")
    
    async def list_invoices(self, page: int = 1, per_page: int = 200, **filters) -> dict:
        """List invoices with filters."""
        params = {"page": page, "per_page": per_page, **filters}
        return await self._request("GET", "invoices", params=params)
    
    async def search_invoice_by_external_id(self, external_id: str) -> dict | None:
        """Search invoice by external ID (Tally voucher number)."""
        resp = await self.list_invoices(per_page=200)
        for inv in resp.get("invoices", []):
            if inv.get("cf_external_id") == external_id:
                return inv
        return None
    
    # ============ BILLS (Purchase) ============
    
    async def create_bill(self, bill_data: dict) -> dict:
        """Create a purchase bill."""
        return await self._request("POST", "bills", json_data=bill_data)
    
    async def get_bill(self, bill_id: str) -> dict:
        """Get a bill by ID."""
        return await self._request("GET", f"bills/{bill_id}")
    
    async def update_bill(self, bill_id: str, bill_data: dict) -> dict:
        """Update a bill."""
        return await self._request("PUT", f"bills/{bill_id}", json_data=bill_data)
    
    async def void_bill(self, bill_id: str) -> dict:
        """Void a bill."""
        return await self._request("POST", f"bills/{bill_id}/status/void")
    
    async def list_bills(self, page: int = 1, per_page: int = 200, **filters) -> dict:
        """List bills."""
        params = {"page": page, "per_page": per_page, **filters}
        return await self._request("GET", "bills", params=params)
    
    async def search_bill_by_external_id(self, external_id: str) -> dict | None:
        """Search bill by external ID."""
        resp = await self.list_bills(per_page=200)
        for bill in resp.get("bills", []):
            if bill.get("cf_external_id") == external_id:
                return bill
        return None
    
    # ============ PAYMENTS (Customer Payments / Vendor Payments) ============
    
    async def create_customer_payment(self, payment_data: dict) -> dict:
        """Record a customer payment (receipt)."""
        return await self._request("POST", "customerpayments", json_data=payment_data)
    
    async def create_vendor_payment(self, payment_data: dict) -> dict:
        """Record a vendor payment."""
        return await self._request("POST", "vendorpayments", json_data=payment_data)
    
    async def list_customer_payments(self, page: int = 1, per_page: int = 200) -> dict:
        return await self._request("GET", "customerpayments", params={"page": page, "per_page": per_page})
    
    async def list_vendor_payments(self, page: int = 1, per_page: int = 200) -> dict:
        return await self._request("GET", "vendorpayments", params={"page": page, "per_page": per_page})
    
    # ============ JOURNALS ============
    
    async def create_journal(self, journal_data: dict) -> dict:
        """Create a journal entry."""
        return await self._request("POST", "journals", json_data=journal_data)
    
    async def get_journal(self, journal_id: str) -> dict:
        return await self._request("GET", f"journals/{journal_id}")
    
    async def list_journals(self, page: int = 1, per_page: int = 200) -> dict:
        return await self._request("GET", "journals", params={"page": page, "per_page": per_page})
    
    async def search_journal_by_external_id(self, external_id: str) -> dict | None:
        resp = await self.list_journals(per_page=200)
        for j in resp.get("journals", []):
            if j.get("cf_external_id") == external_id:
                return j
        return None
    
    # ============ CREDIT NOTES / DEBIT NOTES ============
    
    async def create_credit_note(self, cn_data: dict) -> dict:
        return await self._request("POST", "creditnotes", json_data=cn_data)
    
    async def create_debit_note(self, dn_data: dict) -> dict:
        return await self._request("POST", "debitnotes", json_data=dn_data)
    
    # ============ CHART OF ACCOUNTS ============
    
    async def create_account(self, account_data: dict) -> dict:
        """Create a chart of account."""
        return await self._request("POST", "chartofaccounts", json_data=account_data)
    
    async def get_account(self, account_id: str) -> dict:
        return await self._request("GET", f"chartofaccounts/{account_id}")
    
    async def list_accounts(self, page: int = 1, per_page: int = 200) -> dict:
        return await self._request("GET", "chartofaccounts", params={"page": page, "per_page": per_page})
    
    async def search_account_by_external_id(self, external_id: str) -> dict | None:
        resp = await self.list_accounts(per_page=500)
        for acc in resp.get("chartofaccounts", []):
            if acc.get("cf_external_id") == external_id:
                return acc
        return None
    
    # ============ TRIAL BALANCE & REPORTS ============
    
    async def get_trial_balance(self, from_date: str | None = None, to_date: str | None = None) -> dict:
        """Get trial balance report."""
        params = {}
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date
        return await self._request("GET", "reports/trialbalance", params=params)
    
    async def get_balance_sheet(self, from_date: str | None = None, to_date: str | None = None) -> dict:
        params = {}
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date
        return await self._request("GET", "reports/balancesheet", params=params)
    
    async def get_pl(self, from_date: str | None = None, to_date: str | None = None) -> dict:
        params = {}
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date
        return await self._request("GET", "reports/profitandloss", params=params)
    
    # ============ TAX / GST ============
    
    async def list_taxes(self) -> dict:
        """List tax codes/GST settings."""
        return await self._request("GET", "settings/taxes")
    
    async def get_gst_settings(self) -> dict:
        """Get GST settings for the organization."""
        return await self._request("GET", "settings/gst")
    
    # ============ OAUTH HELPERS ============
    
    @staticmethod
    def get_oauth_url(state: str | None = None) -> str:
        """Generate Zoho OAuth URL for India edition."""
        params = {
            "client_id": settings.zoho_client_id,
            "response_type": "code",
            "redirect_uri": settings.zoho_redirect_uri,
            "scope": "ZohoBooks.fullaccess.all",
            "access_type": "offline",
            "prompt": "consent",
        }
        if state:
            params["state"] = state
        return f"https://accounts.zoho.in/oauth/v2/auth?{urlencode(params)}"
    
    @staticmethod
    async def exchange_code_for_tokens(code: str) -> dict:
        """Exchange authorization code for access/refresh tokens."""
        data = {
            "code": code,
            "client_id": settings.zoho_client_id,
            "client_secret": settings.zoho_client_secret,
            "redirect_uri": settings.zoho_redirect_uri,
            "grant_type": "authorization_code",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post("https://accounts.zoho.in/oauth/v2/token", data=data)
            resp.raise_for_status()
            return resp.json()
    
    async def close(self):
        await self._client.aclose()


# ============ TALLY TO ZOHO MAPPERS ============

def tally_contact_to_zoho(tally_contact: dict, contact_type: str) -> dict:
    """Map Tally ledger/contact to Zoho Books contact."""
    return {
        "contact_name": tally_contact.get("name", ""),
        "contact_type": contact_type,  # "customer" or "vendor"
        "company_name": tally_contact.get("company_name", ""),
        "gst_treatment": tally_contact.get("gst_treatment", "business_gst"),
        "gstin": tally_contact.get("gstin", ""),
        "pan": tally_contact.get("pan", ""),
        "currency_code": tally_contact.get("currency", "INR"),
        "payment_terms": tally_contact.get("payment_terms", 0),
        "payment_terms_label": tally_contact.get("payment_terms_label", ""),
        "billing_address": {
            "address": tally_contact.get("billing_address", ""),
            "city": tally_contact.get("billing_city", ""),
            "state": tally_contact.get("billing_state", ""),
            "zip": tally_contact.get("billing_pincode", ""),
            "country": tally_contact.get("billing_country", "India"),
        },
        "shipping_address": {
            "address": tally_contact.get("shipping_address", ""),
            "city": tally_contact.get("shipping_city", ""),
            "state": tally_contact.get("shipping_state", ""),
            "zip": tally_contact.get("shipping_pincode", ""),
            "country": tally_contact.get("shipping_country", "India"),
        },
        "contact_persons": [
            {
                "first_name": tally_contact.get("contact_person", ""),
                "email": tally_contact.get("email", ""),
                "phone": tally_contact.get("phone", ""),
                "mobile": tally_contact.get("mobile", ""),
                "is_primary_contact": True,
            }
        ] if tally_contact.get("contact_person") else [],
        "cf_external_id": tally_contact.get("tally_ledger_id", ""),
        "cf_tally_guid": tally_contact.get("tally_guid", ""),
    }


def tally_item_to_zoho(tally_item: dict) -> dict:
    """Map Tally stock item to Zoho Books item."""
    return {
        "name": tally_item.get("name", ""),
        "description": tally_item.get("description", ""),
        "rate": float(tally_item.get("rate", 0)),
        "unit": tally_item.get("unit", "Nos"),
        "tax_id": tally_item.get("tax_id", ""),
        "tax_name": tally_item.get("tax_name", "GST"),
        "tax_percentage": float(tally_item.get("tax_percentage", 18)),
        "hsn_or_sac": tally_item.get("hsn_code", ""),
        "item_type": "sales_and_purchases" if tally_item.get("is_inventory") else "sales",
        "product_type": "goods" if tally_item.get("is_inventory") else "service",
        "sku": tally_item.get("sku", ""),
        "cf_external_id": tally_item.get("tally_stock_item_id", ""),
        "cf_tally_guid": tally_item.get("tally_guid", ""),
    }


def tally_account_to_zoho(tally_account: dict) -> dict:
    """Map Tally ledger/account to Zoho Books chart of accounts."""
    return {
        "account_name": tally_account.get("name", ""),
        "account_type": tally_account.get("account_type", "other_current_liability"),
        "description": tally_account.get("description", ""),
        "currency_code": tally_account.get("currency", "INR"),
        "opening_balance": float(tally_account.get("opening_balance", 0)),
        "cf_external_id": tally_account.get("tally_ledger_id", ""),
        "cf_tally_guid": tally_account.get("tally_guid", ""),
    }


def tally_voucher_to_zoho_invoice(tally_voucher: dict, contact_map: dict, item_map: dict) -> dict:
    """Map Tally sales voucher to Zoho Books invoice."""
    lines = []
    for line in tally_voucher.get("ledger_entries", []):
        item_id = item_map.get(line.get("stock_item_id", ""))
        if item_id:
            lines.append({
                "item_id": item_id,
                "name": line.get("item_name", ""),
                "description": line.get("description", ""),
                "quantity": float(line.get("quantity", 1)),
                "rate": float(line.get("rate", 0)),
                "discount": float(line.get("discount", 0)),
                "tax_id": line.get("tax_id", ""),
            })
    
    contact_id = contact_map.get(tally_voucher.get("party_ledger_id", ""))
    
    return {
        "customer_id": contact_id,
        "date": tally_voucher.get("date", ""),
        "invoice_number": tally_voucher.get("voucher_number", ""),
        "reference_number": tally_voucher.get("narration", ""),
        "due_date": tally_voucher.get("due_date", ""),
        "line_items": lines,
        "notes": tally_voucher.get("narration", ""),
        "terms": tally_voucher.get("terms", ""),
        "cf_external_id": tally_voucher.get("tally_voucher_id", ""),
        "cf_tally_guid": tally_voucher.get("tally_guid", ""),
        "cf_tally_voucher_type": tally_voucher.get("voucher_type", "Sales"),
    }


def tally_voucher_to_zoho_bill(tally_voucher: dict, contact_map: dict, item_map: dict) -> dict:
    """Map Tally purchase voucher to Zoho Books bill."""
    lines = []
    for line in tally_voucher.get("ledger_entries", []):
        item_id = item_map.get(line.get("stock_item_id", ""))
        if item_id:
            lines.append({
                "item_id": item_id,
                "name": line.get("item_name", ""),
                "description": line.get("description", ""),
                "quantity": float(line.get("quantity", 1)),
                "rate": float(line.get("rate", 0)),
                "discount": float(line.get("discount", 0)),
                "tax_id": line.get("tax_id", ""),
            })
    
    contact_id = contact_map.get(tally_voucher.get("party_ledger_id", ""))
    
    return {
        "vendor_id": contact_id,
        "date": tally_voucher.get("date", ""),
        "bill_number": tally_voucher.get("voucher_number", ""),
        "due_date": tally_voucher.get("due_date", ""),
        "line_items": lines,
        "notes": tally_voucher.get("narration", ""),
        "cf_external_id": tally_voucher.get("tally_voucher_id", ""),
        "cf_tally_guid": tally_voucher.get("tally_guid", ""),
        "cf_tally_voucher_type": tally_voucher.get("voucher_type", "Purchase"),
    }


def tally_voucher_to_zoho_journal(tally_voucher: dict, account_map: dict) -> dict:
    """Map Tally journal voucher to Zoho Books journal."""
    lines = []
    for entry in tally_voucher.get("ledger_entries", []):
        account_id = account_map.get(entry.get("ledger_id", ""))
        if account_id:
            lines.append({
                "account_id": account_id,
                "debit_or_credit": entry.get("type", "debit").lower(),
                "amount": float(entry.get("amount", 0)),
                "description": entry.get("narration", ""),
            })
    
    return {
        "date": tally_voucher.get("date", ""),
        "reference_number": tally_voucher.get("voucher_number", ""),
        "notes": tally_voucher.get("narration", ""),
        "journal_lines": lines,
        "cf_external_id": tally_voucher.get("tally_voucher_id", ""),
        "cf_tally_guid": tally_voucher.get("tally_guid", ""),
        "cf_tally_voucher_type": tally_voucher.get("voucher_type", "Journal"),
    }


def tally_payment_to_zoho(tally_payment: dict, contact_map: dict, account_map: dict) -> dict:
    """Map Tally payment/receipt to Zoho customer/vendor payment."""
    contact_id = contact_map.get(tally_payment.get("party_ledger_id", ""))
    account_id = account_map.get(tally_payment.get("bank_ledger_id", ""))
    
    base = {
        "date": tally_payment.get("date", ""),
        "amount": float(tally_payment.get("amount", 0)),
        "reference_number": tally_payment.get("voucher_number", ""),
        "description": tally_payment.get("narration", ""),
        "paid_through_account_id": account_id,
        "cf_external_id": tally_payment.get("tally_voucher_id", ""),
        "cf_tally_guid": tally_payment.get("tally_guid", ""),
    }
    
    if tally_payment.get("voucher_type") == "Receipt":
        base["customer_id"] = contact_id
    else:
        base["vendor_id"] = contact_id
    
    return base


def tally_credit_note_to_zoho(tally_cn: dict, contact_map: dict, item_map: dict) -> dict:
    """Map Tally credit note to Zoho credit note."""
    lines = []
    for line in tally_cn.get("ledger_entries", []):
        item_id = item_map.get(line.get("stock_item_id", ""))
        if item_id:
            lines.append({
                "item_id": item_id,
                "name": line.get("item_name", ""),
                "quantity": float(line.get("quantity", 1)),
                "rate": float(line.get("rate", 0)),
                "tax_id": line.get("tax_id", ""),
            })
    
    contact_id = contact_map.get(tally_cn.get("party_ledger_id", ""))
    
    return {
        "customer_id": contact_id,
        "date": tally_cn.get("date", ""),
        "creditnote_number": tally_cn.get("voucher_number", ""),
        "line_items": lines,
        "notes": tally_cn.get("narration", ""),
        "cf_external_id": tally_cn.get("tally_voucher_id", ""),
        "cf_tally_guid": tally_cn.get("tally_guid", ""),
    }


def tally_debit_note_to_zoho(tally_dn: dict, contact_map: dict, item_map: dict) -> dict:
    """Map Tally debit note to Zoho debit note."""
    lines = []
    for line in tally_dn.get("ledger_entries", []):
        item_id = item_map.get(line.get("stock_item_id", ""))
        if item_id:
            lines.append({
                "item_id": item_id,
                "name": line.get("item_name", ""),
                "quantity": float(line.get("quantity", 1)),
                "rate": float(line.get("rate", 0)),
                "tax_id": line.get("tax_id", ""),
            })
    
    contact_id = contact_map.get(tally_dn.get("party_ledger_id", ""))
    
    return {
        "vendor_id": contact_id,
        "date": tally_dn.get("date", ""),
        "debitnote_number": tally_dn.get("voucher_number", ""),
        "line_items": lines,
        "notes": tally_dn.get("narration", ""),
        "cf_external_id": tally_dn.get("tally_voucher_id", ""),
        "cf_tally_guid": tally_dn.get("tally_guid", ""),
    }
