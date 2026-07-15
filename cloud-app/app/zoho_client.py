import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from .config import settings
from .crypto import decrypt, encrypt
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

        # Org-level throttle: enforce a minimum spacing between API calls.
        rate = max(1, settings.zoho_rate_limit_per_min)
        self._min_interval = 60.0 / rate
        self._last_request_at = 0.0
        self._rate_lock = asyncio.Lock()

    async def _throttle(self) -> None:
        """Space out requests to respect Zoho's per-org rate limit."""
        async with self._rate_lock:
            loop = asyncio.get_event_loop()
            wait = self._min_interval - (loop.time() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = loop.time()

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
                access_token=decrypt(token_record.access_token),
                refresh_token=decrypt(token_record.refresh_token),
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
            "refresh_token": decrypt(token_record.refresh_token),
            "client_id": settings.zoho_client_id,
            "client_secret": settings.zoho_client_secret,
            "grant_type": "refresh_token",
        }

        resp = await self._client.post(self.TOKEN_URL, data=data)
        resp.raise_for_status()
        token_data = resp.json()

        expires_at = int(time.time()) + token_data.get("expires_in", 3600)
        new_refresh = token_data.get("refresh_token") or decrypt(token_record.refresh_token)

        # Update DB (tokens encrypted at rest)
        token_record.access_token = encrypt(token_data["access_token"])
        token_record.expires_at = expires_at
        token_record.refresh_token = encrypt(new_refresh)
        token_record.scope = token_data.get("scope", "")
        token_record.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.db.commit()

        self._tokens = ZohoTokens(
            access_token=token_data["access_token"],
            refresh_token=new_refresh,
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

        await self._throttle()
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

    async def _find_by_external_id(self, endpoint: str, list_key: str, external_id: str) -> dict | None:
        """
        Page through a Zoho list endpoint looking for a record whose
        ``cf_external_id`` matches. Walks every page (Zoho caps per_page at 200),
        so it is correct for organizations with more than one page of records.
        """
        page = 1
        while True:
            resp = await self._request("GET", endpoint, params={"page": page, "per_page": 200})
            for record in resp.get(list_key, []):
                if record.get("cf_external_id") == external_id:
                    return record
            if not resp.get("page_context", {}).get("has_more_page"):
                return None
            page += 1

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
        """Search contact by external id (Tally ledger GUID) across all pages."""
        return await self._find_by_external_id("contacts", "contacts", external_id)
    
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
        """Search item by external ID (Tally stock item GUID) across all pages."""
        return await self._find_by_external_id("items", "items", external_id)
    
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
        """Search invoice by external ID (Tally voucher GUID) across all pages."""
        return await self._find_by_external_id("invoices", "invoices", external_id)
    
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
        """Search bill by external ID (Tally voucher GUID) across all pages."""
        return await self._find_by_external_id("bills", "bills", external_id)
    
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
        return await self._find_by_external_id("journals", "journals", external_id)
    
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
        return await self._find_by_external_id("chartofaccounts", "chartofaccounts", external_id)
    
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

