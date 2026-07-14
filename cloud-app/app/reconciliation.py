"""
Reconciliation Service - Trial Balance Matching between Tally and Zoho Books.
"""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from .models import AuditEvent, ReconciliationRun
from .zoho_client import ZohoBooksClient


class ReconciliationService:
    """Service for comparing Tally and Zoho Books data to ensure Trial Balance matches."""
    
    TOLERANCE = Decimal("0.01")  # 1 paisa tolerance for floating point
    
    def __init__(self, db: Session):
        self.db = db
    
    async def run_trial_balance_reconciliation(
        self,
        tenant_id: str,
        period_from: str,
        period_to: str,
        zoho_client: ZohoBooksClient,
        tally_trial_balance: dict,
    ) -> ReconciliationRun:
        """
        Run trial balance reconciliation between Tally and Zoho Books.
        
        Args:
            tenant_id: Tenant ID
            period_from: Start date (YYYY-MM-DD)
            period_to: End date (YYYY-MM-DD)
            zoho_client: Authenticated Zoho Books client
            tally_trial_balance: Parsed Tally trial balance data
            
        Returns:
            ReconciliationRun with results
        """
        run = ReconciliationRun(
            id=str(uuid4()),
            tenant_id=tenant_id,
            run_type="TRIAL_BALANCE",
            period_from=period_from,
            period_to=period_to,
            status="RUNNING",
        )
        self.db.add(run)
        self.db.commit()
        
        try:
            # Get Zoho Books trial balance
            zoho_tb = await zoho_client.get_trial_balance(
                from_date=period_from,
                to_date=period_to,
            )
            
            # Parse both trial balances
            tally_ledgers = self._parse_tally_trial_balance(tally_trial_balance)
            zoho_ledgers = self._parse_zoho_trial_balance(zoho_tb)
            
            # Compare
            mismatches = self._compare_trial_balances(tally_ledgers, zoho_ledgers)
            
            # Calculate totals
            tally_debit: Decimal = sum(
                (Decimal(str(v.get("debit", 0))) for v in tally_ledgers.values()),
                Decimal("0")
            )
            tally_credit: Decimal = sum(
                (Decimal(str(v.get("credit", 0))) for v in tally_ledgers.values()),
                Decimal("0")
            )
            zoho_debit: Decimal = sum(
                (Decimal(str(v.get("debit", 0))) for v in zoho_ledgers.values()),
                Decimal("0")
            )
            zoho_credit: Decimal = sum(
                (Decimal(str(v.get("credit", 0))) for v in zoho_ledgers.values()),
                Decimal("0")
            )
            
            run.tally_total_debit = float(tally_debit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            run.tally_total_credit = float(tally_credit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            run.zoho_total_debit = float(zoho_debit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            run.zoho_total_credit = float(zoho_credit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            run.mismatch_count = len(mismatches)
            run.mismatch_details = {"mismatches": mismatches}
            run.status = "MISMATCH" if mismatches else "COMPLETED"
            run.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            
            self.db.commit()
            self.db.refresh(run)
            
            # Emit audit event
            self._emit_audit(
                tenant_id,
                "RECONCILIATION",
                "TRIAL_BALANCE_COMPLETED",
                {
                    "run_id": run.id,
                    "period_from": period_from,
                    "period_to": period_to,
                    "status": run.status,
                    "mismatch_count": len(mismatches),
                },
            )
            
            return run
            
        except Exception as e:
            run.status = "FAILED"
            run.error_message = str(e)
            run.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            self.db.commit()
            
            self._emit_audit(
                tenant_id,
                "RECONCILIATION",
                "TRIAL_BALANCE_FAILED",
                {"run_id": run.id, "error": str(e)},
            )
            raise
    
    def _parse_tally_trial_balance(self, tally_data: dict) -> dict[str, dict]:
        """Parse Tally trial balance XML/JSON to dict of ledger_name -> {debit, credit}."""
        ledgers = {}
        
        # Tally XML structure: <TRIALBALANCE><LEDGER><NAME>...</NAME><DEBIT>...</DEBIT><CREDIT>...</CREDIT></LEDGER></TRIALBALANCE>
        for ledger in tally_data.get("TRIALBALANCE", {}).get("LEDGER", []):
            name = ledger.get("NAME", "").strip()
            debit = self._safe_decimal(ledger.get("DEBIT", "0"))
            credit = self._safe_decimal(ledger.get("CREDIT", "0"))
            
            if name:
                ledgers[name] = {
                    "debit": float(debit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                    "credit": float(credit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                }
        
        return ledgers
    
    def _parse_zoho_trial_balance(self, zoho_data: dict) -> dict[str, dict]:
        """Parse Zoho Books trial balance report to dict."""
        ledgers = {}
        
        # Zoho Books trial balance structure
        for row in zoho_data.get("trial_balance", []):
            name = row.get("account_name", "").strip()
            debit = self._safe_decimal(row.get("debit", "0"))
            credit = self._safe_decimal(row.get("credit", "0"))
            
            if name:
                ledgers[name] = {
                    "debit": float(debit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                    "credit": float(credit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                }
        
        return ledgers
    
    def _compare_trial_balances(
        self,
        tally_ledgers: dict[str, dict],
        zoho_ledgers: dict[str, dict],
    ) -> list[dict]:
        """Compare two trial balances and return mismatches."""
        mismatches = []
        all_names = set(tally_ledgers.keys()) | set(zoho_ledgers.keys())
        
        for name in sorted(all_names):
            tally = tally_ledgers.get(name, {"debit": 0.0, "credit": 0.0})
            zoho = zoho_ledgers.get(name, {"debit": 0.0, "credit": 0.0})
            
            debit_diff = abs(tally["debit"] - zoho["debit"])
            credit_diff = abs(tally["credit"] - zoho["credit"])
            
            if debit_diff > self.TOLERANCE or credit_diff > self.TOLERANCE:
                mismatches.append({
                    "ledger_name": name,
                    "tally_debit": tally["debit"],
                    "tally_credit": tally["credit"],
                    "zoho_debit": zoho["debit"],
                    "zoho_credit": zoho["credit"],
                    "debit_difference": round(debit_diff, 2),
                    "credit_difference": round(credit_diff, 2),
                })
        
        return mismatches
    
    async def run_open_invoices_reconciliation(
        self,
        tenant_id: str,
        period_from: str,
        period_to: str,
        zoho_client: ZohoBooksClient,
        tally_open_invoices: list[dict],
    ) -> ReconciliationRun:
        """Reconcile open invoices between Tally and Zoho Books."""
        run = ReconciliationRun(
            id=str(uuid4()),
            tenant_id=tenant_id,
            run_type="OPEN_INVOICES",
            period_from=period_from,
            period_to=period_to,
            status="RUNNING",
        )
        self.db.add(run)
        self.db.commit()
        
        try:
            zoho_invoices = await zoho_client.list_invoices(
                per_page=500,
                status="open",
                from_date=period_from,
                to_date=period_to,
            )
            
            zoho_map: dict[str, dict[str, Any]] = {
                inv.get("invoice_number", ""): {
                    "amount": float(inv.get("total", 0)),
                    "balance": float(inv.get("balance", 0)),
                    "customer": inv.get("customer_name", ""),
                    "date": inv.get("date", ""),
                }
                for inv in zoho_invoices.get("invoices", [])
            }
            
            mismatches: list[dict[str, Any]] = []
            for tally_inv in tally_open_invoices:
                inv_num = tally_inv.get("voucher_number", "")
                zoho_inv = zoho_map.get(inv_num)
                
                if not zoho_inv:
                    mismatches.append({
                        "type": "MISSING_IN_ZOHO",
                        "tally_invoice": tally_inv,
                    })
                else:
                    tally_amount = float(tally_inv.get("total_amount", 0))
                    zoho_amount = zoho_inv["amount"]
                    
                    if abs(tally_amount - zoho_amount) > self.TOLERANCE:
                        mismatches.append({
                            "type": "AMOUNT_MISMATCH",
                            "invoice_number": inv_num,
                            "tally_amount": tally_amount,
                            "zoho_amount": zoho_amount,
                            "difference": round(abs(tally_amount - zoho_amount), 2),
                        })
            
            # Check for invoices in Zoho but not in Tally
            tally_nums = {inv.get("voucher_number", "") for inv in tally_open_invoices}
            for zoho_num, zoho_inv in zoho_map.items():
                if zoho_num not in tally_nums:
                    mismatches.append({
                        "type": "MISSING_IN_TALLY",
                        "zoho_invoice": zoho_inv,
                    })
            
            run.mismatch_count = len(mismatches)
            run.mismatch_details = {"mismatches": mismatches}
            run.status = "MISMATCH" if mismatches else "COMPLETED"
            run.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            
            self.db.commit()
            self.db.refresh(run)
            
            self._emit_audit(
                tenant_id,
                "RECONCILIATION",
                "OPEN_INVOICES_COMPLETED",
                {"run_id": run.id, "mismatch_count": len(mismatches)},
            )
            
            return run
            
        except Exception as e:
            run.status = "FAILED"
            run.error_message = str(e)
            run.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            self.db.commit()
            raise
    
    async def run_payments_reconciliation(
        self,
        tenant_id: str,
        period_from: str,
        period_to: str,
        zoho_client: ZohoBooksClient,
        tally_payments: list[dict],
    ) -> ReconciliationRun:
        """Reconcile customer/vendor payments between Tally and Zoho Books."""
        run = ReconciliationRun(
            id=str(uuid4()),
            tenant_id=tenant_id,
            run_type="PAYMENTS",
            period_from=period_from,
            period_to=period_to,
            status="RUNNING",
        )
        self.db.add(run)
        self.db.commit()
        
        try:
            # Get Zoho customer payments
            zoho_cust_payments = await zoho_client.list_customer_payments(per_page=500)
            zoho_vend_payments = await zoho_client.list_vendor_payments(per_page=500)
            
            zoho_payments: dict[str, dict[str, Any]] = {}
            for p in zoho_cust_payments.get("customerpayments", []):
                key = f"RECEIPT:{p.get('payment_number', '')}"
                zoho_payments[key] = {
                    "amount": float(p.get("amount", 0)),
                    "date": p.get("date", ""),
                    "customer": p.get("customer_name", ""),
                    "reference": p.get("reference_number", ""),
                }
            
            for p in zoho_vend_payments.get("vendorpayments", []):
                key = f"PAYMENT:{p.get('payment_number', '')}"
                zoho_payments[key] = {
                    "amount": float(p.get("amount", 0)),
                    "date": p.get("date", ""),
                    "vendor": p.get("vendor_name", ""),
                    "reference": p.get("reference_number", ""),
                }
            
            mismatches: list[dict[str, Any]] = []
            for tally_pay in tally_payments:
                vtype = tally_pay.get("voucher_type", "").upper()
                vnum = tally_pay.get("voucher_number", "")
                key = f"{vtype}:{vnum}"
                
                zoho_pay = zoho_payments.get(key)
                tally_amt = float(tally_pay.get("amount", 0))
                
                if not zoho_pay:
                    mismatches.append({
                        "type": "MISSING_IN_ZOHO",
                        "tally_payment": tally_pay,
                    })
                else:
                    zoho_amt = zoho_pay["amount"]
                    if abs(tally_amt - zoho_amt) > self.TOLERANCE:
                        mismatches.append({
                            "type": "AMOUNT_MISMATCH",
                            "payment_number": vnum,
                            "payment_type": vtype,
                            "tally_amount": tally_amt,
                            "zoho_amount": zoho_amt,
                            "difference": round(abs(tally_amt - zoho_amt), 2),
                        })
            
            # Check for payments in Zoho but not Tally
            tally_keys = {f"{p.get('voucher_type', '').upper()}:{p.get('voucher_number', '')}" for p in tally_payments}
            for key, zoho_pay in zoho_payments.items():
                if key not in tally_keys:
                    mismatches.append({
                        "type": "MISSING_IN_TALLY",
                        "zoho_payment": zoho_pay,
                    })
            
            run.mismatch_count = len(mismatches)
            run.mismatch_details = {"mismatches": mismatches}
            run.status = "MISMATCH" if mismatches else "COMPLETED"
            run.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            
            self.db.commit()
            self.db.refresh(run)
            
            self._emit_audit(
                tenant_id,
                "RECONCILIATION",
                "PAYMENTS_COMPLETED",
                {"run_id": run.id, "mismatch_count": len(mismatches)},
            )
            
            return run
            
        except Exception as e:
            run.status = "FAILED"
            run.error_message = str(e)
            run.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            self.db.commit()
            raise
    
    def _safe_decimal(self, value: str | int | float) -> Decimal:
        """Safely convert value to Decimal."""
        try:
            return Decimal(str(value).replace(",", ""))
        except Exception:
            return Decimal("0")
    
    def _emit_audit(
        self,
        tenant_id: str,
        category: str,
        action: str,
        payload: dict,
    ) -> None:
        """Emit audit event."""
        self.db.add(AuditEvent(
            tenant_id=tenant_id,
            category=category,
            action=action,
            payload=payload,
        ))
        self.db.commit()


def parse_tally_trial_balance_xml(xml_content: str) -> dict:
    """Parse Tally Trial Balance XML export to dict."""
    import xml.etree.ElementTree as ET
    
    root = ET.fromstring(xml_content)
    result: dict[str, Any] = {"TRIALBALANCE": {"LEDGER": []}}
    
    for ledger in root.findall(".//LEDGER"):
        name = ledger.findtext("NAME", "")
        debit = ledger.findtext("DEBIT", "0")
        credit = ledger.findtext("CREDIT", "0")
        result["TRIALBALANCE"]["LEDGER"].append({
            "NAME": name,
            "DEBIT": debit,
            "CREDIT": credit,
        })
    
    return result


def parse_tally_open_invoices_xml(xml_content: str) -> list[dict]:
    """Parse Tally Outstanding/Receivables XML to list of open invoices."""
    import xml.etree.ElementTree as ET
    
    root = ET.fromstring(xml_content)
    invoices = []
    
    for voucher in root.findall(".//VOUCHER"):
        if voucher.get("VCHTYPE") in ("Sales", "Sales Invoice"):
            invoices.append({
                "voucher_number": voucher.findtext("VOUCHERNUMBER", ""),
                "date": voucher.findtext("DATE", ""),
                "party_ledger_name": voucher.findtext("PARTYLEDGERNAME", ""),
                "total_amount": voucher.findtext("TOTALAMOUNT", "0"),
                "outstanding_amount": voucher.findtext("OUTSTANDINGAMOUNT", "0"),
                "voucher_type": voucher.get("VCHTYPE", ""),
            })
    
    return invoices


def parse_tally_payments_xml(xml_content: str) -> list[dict]:
    """Parse Tally Receipt/Payment vouchers XML."""
    import xml.etree.ElementTree as ET
    
    root = ET.fromstring(xml_content)
    payments = []
    
    for voucher in root.findall(".//VOUCHER"):
        vtype = voucher.get("VCHTYPE", "").upper()
        if vtype in ("RECEIPT", "PAYMENT", "CONTRA"):
            payments.append({
                "voucher_number": voucher.findtext("VOUCHERNUMBER", ""),
                "date": voucher.findtext("DATE", ""),
                "voucher_type": vtype,
                "party_ledger_name": voucher.findtext("PARTYLEDGERNAME", ""),
                "amount": voucher.findtext("AMOUNT", "0"),
                "bank_ledger_name": voucher.findtext("BANKLEDGERNAME", ""),
                "narration": voucher.findtext("NARRATION", ""),
            })
    
    return payments
