import pandas as pd
import pytest

from revintel.ingest.quality import ContractViolation, validate

CONTRACT = {
    "columns": {
        "id": {"type": "string", "required": True, "regex": r"^A-\d{3}$"},
        "amount": {"type": "integer", "required": True, "min": 0},
        "status": {"type": "string", "required": True, "allowed": ["paid", "failed"]},
        "priority": {"type": "string", "allowed": ["low", "high"], "case_insensitive": True},
        "ts": {"type": "timestamp"},
    }
}


def frame(**overrides):
    base = {"id": "A-001", "amount": "10", "status": "paid", "priority": "low",
            "ts": "2025-01-01T10:00:00+05:30"}
    return {**base, **overrides}


def test_valid_rows_pass_untouched():
    df = pd.DataFrame([frame(), frame(id="A-002", priority="HIGH")])
    res = validate(df, CONTRACT, max_reject_rate=0.5)
    assert len(res.valid) == 2
    assert res.rejected.empty


@pytest.mark.parametrize("override, reason", [
    ({"id": None}, "id:missing"),
    ({"id": "B-1"}, "id:bad_format"),
    ({"amount": "-5"}, "amount:below_min"),
    ({"amount": "1.5"}, "amount:not_integer"),
    ({"amount": "abc"}, "amount:not_integer"),
    ({"status": "pending"}, "status:not_allowed"),
    ({"ts": "not a date"}, "ts:bad_timestamp"),
])
def test_bad_rows_are_quarantined_with_reason(override, reason):
    df = pd.DataFrame([frame()] * 9 + [frame(**override)])
    res = validate(df, CONTRACT, max_reject_rate=0.5)
    assert len(res.valid) == 9
    assert res.rejected["_reject_reason"].tolist() == [reason]


def test_circuit_breaker_rejects_whole_file():
    df = pd.DataFrame([frame(amount="-1")] * 3 + [frame()])
    with pytest.raises(ContractViolation, match="reject rate"):
        validate(df, CONTRACT, max_reject_rate=0.05)


def test_missing_required_column_rejects_file():
    df = pd.DataFrame([frame()]).drop(columns="amount")
    with pytest.raises(ContractViolation, match="missing required columns"):
        validate(df, CONTRACT, max_reject_rate=0.5)


def test_unexpected_columns_are_dropped_with_warning():
    df = pd.DataFrame([{**frame(), "surprise": 1}])
    res = validate(df, CONTRACT, max_reject_rate=0.5)
    assert "surprise" not in res.valid.columns
    assert any("surprise" in w for w in res.warnings)
