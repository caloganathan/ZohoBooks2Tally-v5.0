"""Smoke tests for the on-prem agent.

The import test guards against a regression where the agent module failed to
import at all (a missing ``tally_voucher_to_sync_payload`` symbol), which took
the entire agent service down on startup.
"""


def test_agent_app_imports():
    import app.main  # noqa: F401  -- import must not raise

    assert app.main.app is not None


def test_voucher_to_sync_payload_maps_sales_invoice():
    from app.tally_http import tally_voucher_to_sync_payload

    voucher = {
        "VCHTYPE": "Sales",
        "GUID": "guid-1",
        "VOUCHERNUMBER": "INV-1",
        "DATE": "20260714",
        "PARTYLEDGERNAME": "Acme",
        "ALLINVENTORYENTRIES": {
            "STOCKITEMNAME": "Widget",
            "BILLEDQTY": "2",
            "RATE": "100",
            "AMOUNT": "200",
        },
        "LEDGERENTRIES": [
            {"LEDGERNAME": "Acme", "AMOUNT": "-236", "DEBITCREDIT": "Debit"},
            {"LEDGERNAME": "Sales", "AMOUNT": "200"},
        ],
    }

    result = tally_voucher_to_sync_payload(voucher)
    assert result["object_type"] == "INVOICE"
    assert result["source_id"] == "guid-1"
    payload = result["payload"]
    assert payload["date"] == "2026-07-14"
    assert payload["party_ledger_name"] == "Acme"
    assert len(payload["inventory_entries"]) == 1
    assert len(payload["ledger_entries"]) == 2
    # Negative Tally amount normalized to positive with explicit debit type.
    assert payload["ledger_entries"][0] == {
        "ledger_name": "Acme",
        "amount": 236.0,
        "type": "debit",
        "narration": "",
    }


def test_voucher_to_sync_payload_unknown_type_defaults_to_journal():
    from app.tally_http import tally_voucher_to_sync_payload

    result = tally_voucher_to_sync_payload({"VCHTYPE": "Contra", "GUID": "g2"})
    assert result["object_type"] == "JOURNAL"
    assert result["source_id"] == "g2"
