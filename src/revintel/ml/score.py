"""
Batch scoring: score every active account at the latest snapshot and explain each score.

Per-account explanations use a counterfactual approach: for each feature, replace the
account's value with the typical value for healthy (retained) accounts and measure how
much the churn probability drops. The features with the biggest drop are that account's
top risk drivers - easy for a Customer Success manager to act on.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import duckdb
import joblib
import numpy as np
import pandas as pd

from revintel.ml.features import (
    BOOLEAN_FEATURES,
    CATEGORICAL_FEATURES,
    FEATURE_LABELS,
    NUMERIC_FEATURES,
    prepare,
)

log = logging.getLogger(__name__)

RISK_TIERS = [(0.30, "high"), (0.15, "medium"), (0.0, "low")]
# features a CSM can actually influence - firmographics are excluded from "drivers"
ACTIONABLE = [
    "seat_utilization", "sessions_per_seat", "core_events_per_session", "dau_trend_vs_prev_3m",
    "active_days", "tickets_3m", "high_priority_tickets_3m", "billing_tickets_3m",
    "low_csat_tickets_3m", "avg_csat_3m", "failed_invoices_3m", "seats_change_3m",
    "mrr_change_3m_pct", "uses_integrations",
]


def risk_tier(p: float) -> str:
    return next(name for threshold, name in RISK_TIERS if p >= threshold)


def reference_profile(history: pd.DataFrame) -> dict:
    """Median/mode feature values of accounts that did NOT churn."""
    healthy = prepare(history[history["churned_within_horizon"] == False])  # noqa: E712
    ref = {c: float(healthy[c].median()) for c in NUMERIC_FEATURES + BOOLEAN_FEATURES}
    ref.update({c: healthy[c].mode().iat[0] for c in CATEGORICAL_FEATURES})
    return ref


def explain(model, X: pd.DataFrame, reference: dict, top_k: int = 3) -> list[list[dict]]:
    base = model.predict_proba(X)[:, 1]
    deltas = {}
    for f in ACTIONABLE:
        Xc = X.copy()
        Xc[f] = reference[f]
        deltas[f] = base - model.predict_proba(Xc)[:, 1]
    D = pd.DataFrame(deltas)
    out = []
    for i in range(len(X)):
        row = D.iloc[i].sort_values(ascending=False)
        drivers = [
            {"feature": f, "label": FEATURE_LABELS[f], "value": _clean(X.iloc[i][f]),
             "typical_healthy_value": _clean(reference[f]), "impact": round(float(v), 4)}
            for f, v in row.items() if v > 0.005
        ][:top_k]
        out.append(drivers)
    return out


def _clean(v):
    if isinstance(v, (np.floating, float)):
        return None if np.isnan(v) else round(float(v), 3)
    return v


def score_latest(con: duckdb.DuckDBPyConnection, model_path: Path) -> pd.DataFrame:
    model = joblib.load(model_path)
    features = con.execute("select * from marts.mart_churn_features").df()
    latest_month = features["snapshot_month"].max()
    current = features[features["snapshot_month"] == latest_month].reset_index(drop=True)
    history = features[features["is_label_observed"].astype(bool)]

    X = prepare(current)
    p = model.predict_proba(X)[:, 1]
    drivers = explain(model, X, reference_profile(history))

    scores = pd.DataFrame({
        "account_id": current["account_id"],
        "snapshot_month": pd.to_datetime(current["snapshot_month"]).dt.date,
        "churn_probability": np.round(p, 4),
        "risk_tier": [risk_tier(x) for x in p],
        "mrr_usd": current["mrr_usd"].astype(float),
        "expected_mrr_loss_usd": np.round(current["mrr_usd"].astype(float) * p, 2),
        "top_drivers": [json.dumps(d) for d in drivers],
    })
    scores["risk_rank"] = scores["expected_mrr_loss_usd"].rank(ascending=False, method="first").astype(int)

    con.execute("create schema if not exists ml")
    con.register("_scores", scores)
    con.execute("create or replace table ml.churn_scores as select * from _scores")
    con.unregister("_scores")
    log.info("scored %s active accounts for %s | high risk: %s | expected MRR loss: $%s",
             f"{len(scores):,}", latest_month, (scores["risk_tier"] == "high").sum(),
             f"{scores['expected_mrr_loss_usd'].sum():,.0f}")
    return scores
