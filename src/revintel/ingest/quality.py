"""Row- and file-level validation against the YAML data contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


class ContractViolation(Exception):
    """Raised when a whole file breaks its contract and must not be loaded."""


@dataclass
class ValidationResult:
    valid: pd.DataFrame
    rejected: pd.DataFrame          # original rows + `_reject_reason`
    warnings: list[str] = field(default_factory=list)

    @property
    def reject_rate(self) -> float:
        total = len(self.valid) + len(self.rejected)
        return len(self.rejected) / total if total else 0.0


def _is_null(s: pd.Series) -> pd.Series:
    return s.isna() | (s.astype("string").str.strip() == "")


def _check_column(s: pd.Series, rule: dict[str, Any]) -> pd.Series:
    """Return a Series of failure reasons ('' = ok) for one column."""
    reasons = pd.Series("", index=s.index, dtype="object")
    null = _is_null(s)
    name = s.name

    if rule.get("required"):
        reasons[null] = f"{name}:missing"

    present = ~null
    typ = rule.get("type", "string")
    numeric = None
    if typ in {"integer", "number"}:
        numeric = pd.to_numeric(s.where(present), errors="coerce")
        bad = present & numeric.isna()
        if typ == "integer":
            bad |= present & numeric.notna() & (numeric % 1 != 0)
        reasons[bad & (reasons == "")] = f"{name}:not_{typ}"
    elif typ == "timestamp":
        parsed = pd.to_datetime(s.where(present), errors="coerce", utc=True, format="mixed")
        reasons[present & parsed.isna() & (reasons == "")] = f"{name}:bad_timestamp"

    if numeric is not None:
        if "min" in rule:
            reasons[(numeric < rule["min"]) & (reasons == "")] = f"{name}:below_min"
        if "max" in rule:
            reasons[(numeric > rule["max"]) & (reasons == "")] = f"{name}:above_max"

    if "allowed" in rule:
        allowed = {str(a) for a in rule["allowed"]}
        vals = s.astype("string").str.strip()
        if rule.get("case_insensitive"):
            allowed = {a.lower() for a in allowed}
            vals = vals.str.lower()
        reasons[present & ~vals.isin(allowed) & (reasons == "")] = f"{name}:not_allowed"

    if "regex" in rule:
        pattern = re.compile(rule["regex"])
        ok = s.astype("string").map(lambda v: bool(pattern.match(v)) if isinstance(v, str) else False)
        reasons[present & ~ok.astype(bool) & (reasons == "")] = f"{name}:bad_format"

    return reasons


def validate(df: pd.DataFrame, contract: dict[str, Any], max_reject_rate: float) -> ValidationResult:
    columns: dict[str, dict] = contract["columns"]
    warnings: list[str] = []

    missing = [c for c, r in columns.items() if c not in df.columns and r.get("required")]
    if missing:
        raise ContractViolation(f"missing required columns: {missing}")

    extra = [c for c in df.columns if c not in columns]
    if extra:
        warnings.append(f"dropping unexpected columns: {extra}")
    for c in columns:
        if c not in df.columns:
            df[c] = None
    df = df[list(columns)].copy()

    reasons = pd.Series("", index=df.index, dtype="object")
    for col, rule in columns.items():
        col_reasons = _check_column(df[col], rule)
        reasons = np.where(reasons == "", col_reasons, reasons)
        reasons = pd.Series(reasons, index=df.index)

    bad = reasons != ""
    rejected = df[bad].copy()
    rejected["_reject_reason"] = reasons[bad]
    result = ValidationResult(valid=df[~bad].copy(), rejected=rejected, warnings=warnings)

    if result.reject_rate > max_reject_rate:
        top = rejected["_reject_reason"].value_counts().head(3).to_dict()
        raise ContractViolation(
            f"reject rate {result.reject_rate:.1%} exceeds {max_reject_rate:.0%} (top reasons: {top})"
        )
    return result
