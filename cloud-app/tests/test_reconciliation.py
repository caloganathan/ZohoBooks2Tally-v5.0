"""Regression tests for reconciliation parsing.

These lock the fix for the trial-balance reconciliation silently discarding the
Tally figures passed by the API layer (which builds a normalized
``{ledger_name: {debit, credit}}`` mapping).
"""
from app.reconciliation import ReconciliationService


def _svc() -> ReconciliationService:
    return ReconciliationService(db=None)  # type: ignore[arg-type]


def test_parse_normalized_tally_trial_balance():
    """The API layer passes a pre-normalized mapping; it must be preserved."""
    normalized = {
        "Cash": {"debit": 1000.0, "credit": 0.0},
        "Sales": {"debit": 0.0, "credit": 1000.0},
    }
    parsed = _svc()._parse_tally_trial_balance(normalized)
    assert parsed == {
        "Cash": {"debit": 1000.0, "credit": 0.0},
        "Sales": {"debit": 0.0, "credit": 1000.0},
    }


def test_parse_raw_xml_tally_trial_balance():
    """The raw XML-derived structure must still parse."""
    raw = {"TRIALBALANCE": {"LEDGER": [
        {"NAME": "Cash", "DEBIT": "1,000", "CREDIT": "0"},
        {"NAME": "Sales", "DEBIT": "0", "CREDIT": "1000"},
    ]}}
    parsed = _svc()._parse_tally_trial_balance(raw)
    assert parsed["Cash"] == {"debit": 1000.0, "credit": 0.0}
    assert parsed["Sales"] == {"debit": 0.0, "credit": 1000.0}


def test_matching_trial_balances_have_no_mismatches():
    svc = _svc()
    tally = svc._parse_tally_trial_balance({"Cash": {"debit": 500.0, "credit": 0.0}})
    zoho = {"Cash": {"debit": 500.0, "credit": 0.0}}
    assert svc._compare_trial_balances(tally, zoho) == []


def test_mismatched_trial_balances_are_detected():
    svc = _svc()
    tally = svc._parse_tally_trial_balance({"Cash": {"debit": 500.0, "credit": 0.0}})
    zoho = {"Cash": {"debit": 400.0, "credit": 0.0}}
    mismatches = svc._compare_trial_balances(tally, zoho)
    assert len(mismatches) == 1
    assert mismatches[0]["ledger_name"] == "Cash"
    assert mismatches[0]["debit_difference"] == 100.0
