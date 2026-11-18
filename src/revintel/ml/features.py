"""Feature definitions shared by training, batch scoring and the online API."""

from __future__ import annotations

import duckdb
import pandas as pd

NUMERIC_FEATURES = [
    "company_size_rank",
    "tenure_months",
    "months_into_annual_term",
    "seats",
    "mrr_usd",
    "mrr_change_3m_pct",
    "seats_change_3m",
    "active_days",
    "seat_utilization",
    "sessions_per_seat",
    "core_events_per_session",
    "dau_trend_vs_prev_3m",
    "tickets_3m",
    "high_priority_tickets_3m",
    "billing_tickets_3m",
    "low_csat_tickets_3m",
    "avg_csat_3m",
    "failed_invoices_3m",
]
BOOLEAN_FEATURES = ["is_annual", "uses_integrations"]
CATEGORICAL_FEATURES = ["plan", "industry", "acquisition_channel", "country_code"]
FEATURES = NUMERIC_FEATURES + BOOLEAN_FEATURES + CATEGORICAL_FEATURES
TARGET = "churned_within_horizon"

# Plain-English names used in API responses and the dashboard.
FEATURE_LABELS = {
    "company_size_rank": "Company size",
    "tenure_months": "Customer tenure",
    "months_into_annual_term": "Position in annual term",
    "seats": "Seat count",
    "mrr_usd": "MRR",
    "mrr_change_3m_pct": "MRR change (3 months)",
    "seats_change_3m": "Seat change (3 months)",
    "active_days": "Active days this month",
    "seat_utilization": "Seat utilisation",
    "sessions_per_seat": "Sessions per seat",
    "core_events_per_session": "Core feature depth",
    "dau_trend_vs_prev_3m": "Usage trend vs prior 3 months",
    "tickets_3m": "Support tickets (3 months)",
    "high_priority_tickets_3m": "High-priority tickets (3 months)",
    "billing_tickets_3m": "Billing tickets (3 months)",
    "low_csat_tickets_3m": "Low-CSAT tickets (3 months)",
    "avg_csat_3m": "Average CSAT (3 months)",
    "failed_invoices_3m": "Failed payments (3 months)",
    "is_annual": "Annual contract",
    "uses_integrations": "Uses integrations",
    "plan": "Plan",
    "industry": "Industry",
    "acquisition_channel": "Acquisition channel",
    "country_code": "Country",
}


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce raw feature columns into model-ready dtypes."""
    X = df[FEATURES].copy()
    for c in NUMERIC_FEATURES:
        X[c] = pd.to_numeric(X[c], errors="coerce").astype("float64")
    for c in BOOLEAN_FEATURES:
        X[c] = X[c].fillna(False).astype("float64")
    for c in CATEGORICAL_FEATURES:
        X[c] = X[c].astype("object").fillna("unknown").astype(str)
    return X


def load_feature_table(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    df = con.execute("select * from marts.mart_churn_features").df()
    df["snapshot_month"] = pd.to_datetime(df["snapshot_month"])
    return df
