"""
Tally HTTP Client for Agent - Communicates with TallyPrime via TDL XML/HTTP endpoints.
"""
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import httpx

from .config import settings


class TallyClient:
    """Client for communicating with TallyPrime via HTTP XML endpoints."""
    
    def __init__(self, base_url: str = "http://localhost:9000"):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=60.0)
    
    async def close(self):
        await self.client.aclose()
    
    def _build_request(self, xml_body: str) -> str:
        """Wrap XML in Tally ENVELOPE format."""
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>TDLExport</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    {xml_body}
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>"""
    
    async def _post_xml(self, xml_content: str) -> str:
        """Send XML request to Tally and return response."""
        headers = {"Content-Type": "text/xml"}
        response = await self.client.post(
            self.base_url,
            content=xml_content.encode("utf-8"),
            headers=headers,
        )
        response.raise_for_status()
        return response.text
    
    # ==================== MASTER EXPORTS ====================
    
    async def export_ledgers(self, from_date: str | None = None) -> str:
        """Export all ledgers (customers, vendors, accounts)."""
        xml = """
        <COLLECTION NAME="LedgerCollection" ISMODIFY="No">
            <TYPE>Ledger</TYPE>
            <FETCH>NAME,GUID,PARENT,OPENINGBALANCE,OPENINGBALANCETYPE,
                MAILINGDETAILS.ADDRESS,MAILINGDETAILS.STATE,MAILINGDETAILS.COUNTRY,
                MAILINGDETAILS.PINCODE,MAILINGDETAILS.EMAIL,MAILINGDETAILS.PHONE,
                MAILINGDETAILS.MOBILE,MAILINGDETAILS.CONTACTPERSON,
                GSTIN,PAN,GSTREGISTRATIONTYPE,STATENAME,CURRENCYNAME,
                CREDITPERIOD,ISCOSTCENTRESON,ISINVENTORYVALUESAFFECTED
            </FETCH>
        </COLLECTION>
        """
        request = self._build_request(xml)
        return await self._post_xml(request)
    
    async def export_stock_items(self, from_date: str | None = None) -> str:
        """Export all stock items (products/services)."""
        xml = """
        <COLLECTION NAME="StockItemCollection" ISMODIFY="No">
            <TYPE>Stock Item</TYPE>
            <FETCH>NAME,GUID,DESCRIPTION,BASEUNITS,STANDARDRATE,PURCHASERATE,
                ISSTOCKITEM,REORDERLEVEL,SKU,
                GSTDETAILS.TAXID,GSTDETAILS.TAXNAME,GSTDETAILS.TAXPERCENTAGE,
                GSTDETAILS.HSNCODE,GSTDETAILS.SACCODE
            </FETCH>
        </COLLECTION>
        """
        request = self._build_request(xml)
        return await self._post_xml(request)
    
    async def export_groups(self) -> str:
        """Export all groups (Chart of Accounts structure)."""
        xml = """
        <COLLECTION NAME="GroupCollection" ISMODIFY="No">
            <TYPE>Group</TYPE>
            <FETCH>NAME,GUID,PARENT,ISREVENUE,ISDEEMEDPOSITIVE,
                AFFECTSGROSSPROFIT,ISSUBLEDGER
            </FETCH>
        </COLLECTION>
        """
        request = self._build_request(xml)
        return await self._post_xml(request)
    
    async def export_godowns(self) -> str:
        """Export all godowns (warehouses)."""
        xml = """
        <COLLECTION NAME="GodownCollection" ISMODIFY="No">
            <TYPE>Godown</TYPE>
            <FETCH>NAME,GUID,PARENT,ADDRESS
            </FETCH>
        </COLLECTION>
        """
        request = self._build_request(xml)
        return await self._post_xml(request)
    
    async def export_units(self) -> str:
        """Export all units of measure."""
        xml = """
        <COLLECTION NAME="UnitCollection" ISMODIFY="No">
            <TYPE>Unit</TYPE>
            <FETCH>NAME,GUID,ISSIMPLEUNIT,BASEUNIT,CONVERSION
            </FETCH>
        </COLLECTION>
        """
        request = self._build_request(xml)
        return await self._post_xml(request)
    
    # ==================== VOUCHER EXPORTS ====================
    
    async def export_vouchers(
        self,
        from_date: str,
        to_date: str | None = None,
        voucher_types: list[str] | None = None,
    ) -> str:
        """Export vouchers for a date range."""
        to_date = to_date or from_date
        
        
        xml = f"""
        <COLLECTION NAME="VoucherCollection" ISMODIFY="No">
            <TYPE>Voucher</TYPE>
            <FETCH>GUID,DATE,VOUCHERNUMBER,NARRATION,REFERENCE,
                PARTYLEDGERNAME,VCHTYPE,DUEDATE,EFFECTIVEDATE,
                ISINVOICE,ISACCOUNTINGVOUCHER,ISINVENTORYVOUCHER,
                PLACEOFSUPPLY,GSTTREATMENT,GSTIN,
                ALLINVENTORYENTRIES.STOCKITEMNAME,
                ALLINVENTORYENTRIES.BILLEDQTY,ALLINVENTORYENTRIES.ACTUALQTY,
                ALLINVENTORYENTRIES.RATE,ALLINVENTORYENTRIES.AMOUNT,
                ALLINVENTORYENTRIES.UNIT,ALLINVENTORYENTRIES.DISCOUNT,
                ALLINVENTORYENTRIES.NARRATION,ALLINVENTORYENTRIES.GUID,
                ALLINVENTORYENTRIES.TAXDETAILS,
                LEDGERENTRIES.LEDGERNAME,LEDGERENTRIES.AMOUNT,
                LEDGERENTRIES.DEBITCREDIT,LEDGERENTRIES.NARRATION,
                LEDGERENTRIES.ISDEEMEDPOSITIVE
            </FETCH>
            <FILTERS>DateFilter</FILTERS>
        </COLLECTION>
        
        <SYSTEM TYPE="Formulae" NAME="DateFilter">
            <EXPR>($Date >= "{from_date}") AND ($Date <= "{to_date}")</EXPR>
        </SYSTEM>
        """
        request = self._build_request(xml)
        return await self._post_xml(request)
    
    async def export_sales_vouchers(self, from_date: str, to_date: str | None = None) -> str:
        """Export only sales vouchers."""
        return await self.export_vouchers(from_date, to_date, ["Sales", "Sales Invoice"])
    
    async def export_purchase_vouchers(self, from_date: str, to_date: str | None = None) -> str:
        """Export only purchase vouchers."""
        return await self.export_vouchers(from_date, to_date, ["Purchase", "Purchase Invoice"])
    
    async def export_payment_vouchers(self, from_date: str, to_date: str | None = None) -> str:
        """Export only payment vouchers."""
        return await self.export_vouchers(from_date, to_date, ["Payment"])
    
    async def export_receipt_vouchers(self, from_date: str, to_date: str | None = None) -> str:
        """Export only receipt vouchers."""
        return await self.export_vouchers(from_date, to_date, ["Receipt"])
    
    async def export_journal_vouchers(self, from_date: str, to_date: str | None = None) -> str:
        """Export only journal vouchers."""
        return await self.export_vouchers(from_date, to_date, ["Journal"])
    
    # ==================== REPORTS ====================
    
    async def export_trial_balance(self, from_date: str, to_date: str | None = None) -> str:
        """Export trial balance for a period."""
        to_date = to_date or from_date
        xml = f"""
        <COLLECTION NAME="TrialBalanceCollection" ISMODIFY="No">
            <TYPE>Trial Balance</TYPE>
            <FETCH>NAME,DEBIT,CREDIT,OPENINGBALANCE,CLOSINGBALANCE</FETCH>
            <FILTERS>PeriodFilter</FILTERS>
        </COLLECTION>
        
        <SYSTEM TYPE="Formulae" NAME="PeriodFilter">
            <EXPR>($Date >= "{from_date}") AND ($Date <= "{to_date}")</EXPR>
        </SYSTEM>
        """
        request = self._build_request(xml)
        return await self._post_xml(request)
    
    async def export_outstanding_receivables(self, as_on_date: str | None = None) -> str:
        """Export outstanding receivables (customer ledgers with balances)."""
        date_filter = f'($Date <= "{as_on_date}")' if as_on_date else "1=1"
        xml = f"""
        <COLLECTION NAME="OutstandingReceivables" ISMODIFY="No">
            <TYPE>Ledger</TYPE>
            <FETCH>NAME,GUID,CLOSINGBALANCE,OPENINGBALANCE,PARENT,
                MAILINGDETAILS,CONTACTPERSON,GSTIN
            </FETCH>
            <FILTERS>SundryDebtorsFilter</FILTERS>
        </COLLECTION>
        
        <SYSTEM TYPE="Formulae" NAME="SundryDebtorsFilter">
            <EXPR>($Parent = "Sundry Debtors") AND ({date_filter})</EXPR>
        </SYSTEM>
        """
        request = self._build_request(xml)
        return await self._post_xml(request)
    
    async def export_outstanding_payables(self, as_on_date: str | None = None) -> str:
        """Export outstanding payables (vendor ledgers with balances)."""
        date_filter = f'($Date <= "{as_on_date}")' if as_on_date else "1=1"
        xml = f"""
        <COLLECTION NAME="OutstandingPayables" ISMODIFY="No">
            <TYPE>Ledger</TYPE>
            <FETCH>NAME,GUID,CLOSINGBALANCE,OPENINGBALANCE,PARENT,
                MAILINGDETAILS,CONTACTPERSON,GSTIN
            </FETCH>
            <FILTERS>SundryCreditorsFilter</FILTERS>
        </COLLECTION>
        
        <SYSTEM TYPE="Formulae" NAME="SundryCreditorsFilter">
            <EXPR>($Parent = "Sundry Creditors") AND ({date_filter})</EXPR>
        </SYSTEM>
        """
        request = self._build_request(xml)
        return await self._post_xml(request)
    
    # ==================== CHANGED OBJECTS (for incremental sync) ====================
    
    async def export_changed_ledgers(self, since_date: str) -> str:
        """Export ledgers modified since a date."""
        xml = f"""
        <COLLECTION NAME="ChangedLedgers" ISMODIFY="No">
            <TYPE>Ledger</TYPE>
            <FETCH>NAME,GUID,PARENT,OPENINGBALANCE,LASTMODIFIED,
                MAILINGDETAILS,GSTIN,PAN
            </FETCH>
            <FILTERS>ModifiedFilter</FILTERS>
        </COLLECTION>
        
        <SYSTEM TYPE="Formulae" NAME="ModifiedFilter">
            <EXPR>$LastModified >= "{since_date}"</EXPR>
        </SYSTEM>
        """
        request = self._build_request(xml)
        return await self._post_xml(request)
    
    async def export_changed_stock_items(self, since_date: str) -> str:
        """Export stock items modified since a date."""
        xml = f"""
        <COLLECTION NAME="ChangedStockItems" ISMODIFY="No">
            <TYPE>Stock Item</TYPE>
            <FETCH>NAME,GUID,DESCRIPTION,BASEUNITS,STANDARDRATE,LASTMODIFIED,
                GSTDETAILS
            </FETCH>
            <FILTERS>ModifiedFilter</FILTERS>
        </COLLECTION>
        
        <SYSTEM TYPE="Formulae" NAME="ModifiedFilter">
            <EXPR>$LastModified >= "{since_date}"</EXPR>
        </SYSTEM>
        """
        request = self._build_request(xml)
        return await self._post_xml(request)
    
    async def export_changed_vouchers(self, since_date: str) -> str:
        """Export vouchers modified since a date."""
        xml = f"""
        <COLLECTION NAME="ChangedVouchers" ISMODIFY="No">
            <TYPE>Voucher</TYPE>
            <FETCH>GUID,DATE,VOUCHERNUMBER,NARRATION,REFERENCE,
                PARTYLEDGERNAME,VCHTYPE,LASTMODIFIED,
                ALLINVENTORYENTRIES,LEDGERENTRIES
            </FETCH>
            <FILTERS>ModifiedFilter</FILTERS>
        </COLLECTION>
        
        <SYSTEM TYPE="Formulae" NAME="ModifiedFilter">
            <EXPR>$LastModified >= "{since_date}"</EXPR>
        </SYSTEM>
        """
        request = self._build_request(xml)
        return await self._post_xml(request)
    
    # ==================== IMPORT (Write to Tally) ====================
    
    async def import_voucher(self, voucher_xml: str) -> str:
        """Import a voucher into Tally."""
        # This requires a TDL import definition
        xml = f"""
        <IMPORT>
            <REQUESTDESC>
                <REPORTNAME>Voucher Import</REPORTNAME>
                <STATICVARIABLES>
                    <SVCURRENTCOMPANY>{settings.tally_company or ""}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
            <REQUESTDATA>
                {voucher_xml}
            </REQUESTDATA>
        </IMPORT>
        """
        request = self._build_request(xml)
        return await self._post_xml(request)
    
    async def import_master(self, master_xml: str, master_type: str) -> str:
        """Import a master (ledger, stock item, etc.) into Tally."""
        xml = f"""
        <IMPORT>
            <REQUESTDESC>
                <REPORTNAME>{master_type} Import</REPORTNAME>
                <STATICVARIABLES>
                    <SVCURRENTCOMPANY>{settings.tally_company or ""}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
            <REQUESTDATA>
                {master_xml}
            </REQUESTDATA>
        </IMPORT>
        """
        request = self._build_request(xml)
        return await self._post_xml(request)


# ==================== XML PARSING UTILITIES ====================

def parse_tally_xml_response(xml_string: str) -> dict[str, Any]:
    """Parse Tally XML response into Python dict."""
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError as e:
        raise ValueError(f"Invalid XML response: {e}")
    
    def elem_to_dict(elem: ET.Element) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for child in elem:
            if len(child) == 0:
                tag = child.tag
                text: str | None = child.text
                if tag in result:
                    if not isinstance(result[tag], list):
                        result[tag] = [result[tag]]
                    result[tag].append(text)
                else:
                    result[tag] = text
            else:
                tag = child.tag
                child_dict = elem_to_dict(child)
                if tag in result:
                    if not isinstance(result[tag], list):
                        result[tag] = [result[tag]]
                    result[tag].append(child_dict)
                else:
                    result[tag] = child_dict
        return result
    
    # Navigate to DATA/TALLYMESSAGE
    envelope = root.find("BODY/DATA")
    if envelope is not None:
        return elem_to_dict(envelope)
    return {}


def extract_tally_objects(parsed_xml: dict, object_type: str) -> list[dict]:
    """Extract specific object types from parsed Tally XML."""
    objects: list[dict] = []
    
    # Tally messages are in TALLYMESSAGE
    messages: list[dict] | dict = parsed_xml.get("TALLYMESSAGE", [])
    if not isinstance(messages, list):
        messages = [messages]
    
    for msg in messages:
        if isinstance(msg, dict) and object_type in msg:
            obj = msg[object_type]
            if isinstance(obj, list):
                objects.extend(obj)
            else:
                objects.append(obj)
    
    return objects


def parse_tally_date(date_str: str | None) -> str | None:
    """Parse Tally date format (YYYYMMDD or DD-MM-YYYY) to YYYY-MM-DD."""
    if not date_str:
        return None
    
    # YYYYMMDD
    if len(date_str) == 8 and date_str.isdigit():
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    
    # DD-MM-YYYY
    try:
        return datetime.strptime(date_str, "%d-%m-%Y").strftime("%Y-%m-%d")
    except ValueError:
        pass
    
    # YYYY-MM-DD
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