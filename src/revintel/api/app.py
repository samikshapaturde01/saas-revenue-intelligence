"""
Churn Risk Scoring API.

Serves batch scores from the warehouse (with per-account risk drivers) and real-time
scoring for feature payloads sent by other systems (e.g. the CRM).

Run:  uvicorn revintel.api.app:app --reload
Docs: http://localhost:8000/docs
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager, suppress
from datetime import date
from functools import lru_cache
from typing import Literal

import duckdb
import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from revintel import __version__
from revintel.config import get_settings
from revintel.ml.features import prepare
from revintel.ml.score import risk_tier

Tier = Literal["high", "medium", "low"]


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class Driver(BaseModel):
    feature: str
    label: str
    value: float | str | bool | None
    typical_healthy_value: float | str | bool | None
    impact: float = Field(description="Drop in churn probability if this feature looked healthy")


class AccountRisk(BaseModel):
    account_id: str
    company_name: str
    snapshot_month: date
    churn_probability: float
    risk_tier: Tier
    mrr_usd: float
    expected_mrr_loss_usd: float
    risk_rank: int
    plan: str | None
    industry: str
    top_drivers: list[Driver]


class AccountFeatures(BaseModel):
    """Feature payload for real-time scoring. Mirrors marts.mart_churn_features."""

    plan: Literal["starter", "growth", "enterprise"]
    industry: str = "Software"
    acquisition_channel: str = "organic"
    country_code: str = "US"
    company_size_rank: int = Field(2, ge=1, le=5)
    tenure_months: int = Field(..., ge=0)
    is_annual: bool = False
    months_into_annual_term: int | None = Field(None, ge=0, le=11)
    seats: int = Field(..., ge=1)
    mrr_usd: float = Field(..., ge=0)
    mrr_change_3m_pct: float = 0.0
    seats_change_3m: int = 0
    active_days: int = Field(..., ge=0, le=31)
    seat_utilization: float = Field(..., ge=0)
    sessions_per_seat: float = Field(..., ge=0)
    core_events_per_session: float = Field(..., ge=0)
    dau_trend_vs_prev_3m: float = 1.0
    uses_integrations: bool = False
    tickets_3m: int = 0
    high_priority_tickets_3m: int = 0
    billing_tickets_3m: int = 0
    low_csat_tickets_3m: int = 0
    avg_csat_3m: float | None = Field(None, ge=1, le=5)
    failed_invoices_3m: int = 0

    model_config = {
        "json_schema_extra": {
            "example": {
                "plan": "growth", "industry": "Retail", "acquisition_channel": "paid_search",
                "country_code": "GB", "company_size_rank": 2, "tenure_months": 8, "is_annual": False,
                "seats": 24, "mrr_usd": 600, "mrr_change_3m_pct": -0.15, "seats_change_3m": -4,
                "active_days": 14, "seat_utilization": 0.18, "sessions_per_seat": 6.5,
                "core_events_per_session": 2.4, "dau_trend_vs_prev_3m": 0.7, "uses_integrations": False,
                "tickets_3m": 4, "high_priority_tickets_3m": 1, "billing_tickets_3m": 0,
                "low_csat_tickets_3m": 2, "avg_csat_3m": 2.5, "failed_invoices_3m": 0,
            }
        }
    }


class ScoreResponse(BaseModel):
    churn_probability: float
    risk_tier: Tier
    expected_mrr_loss_usd: float
    model_version: str


class KpiSummary(BaseModel):
    month: date
    mrr_usd: float
    arr_usd: float
    active_customers: int
    logo_churn_rate: float
    net_mrr_retention_rate: float
    accounts_high_risk: int
    expected_mrr_loss_usd: float


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def get_model():
    path = get_settings().path("artifacts") / "model" / "churn_model.joblib"
    if not path.exists():
        raise RuntimeError(f"model not found at {path} - run `revintel train` first")
    return joblib.load(path)


def query(sql: str, params: list | None = None) -> pd.DataFrame:
    with duckdb.connect(str(get_settings().warehouse_path), read_only=True) as con:
        return con.execute(sql, params or []).df()


@asynccontextmanager
async def lifespan(_: FastAPI):
    with suppress(RuntimeError):  # missing model is surfaced via /health, not a crash
        get_model()
    yield


app = FastAPI(
    title="Churn Risk Scoring API",
    version=__version__,
    description="Churn probabilities, revenue at risk and risk drivers for every customer account.",
    lifespan=lifespan,
)

_RISK_SQL = """
    select s.*, a.company_name, a.current_plan as plan, a.industry
    from ml.churn_scores s
    join marts.dim_accounts a using (account_id)
"""


def _to_risk(row: pd.Series) -> AccountRisk:
    return AccountRisk(
        account_id=row["account_id"], company_name=row["company_name"],
        snapshot_month=row["snapshot_month"], churn_probability=row["churn_probability"],
        risk_tier=row["risk_tier"], mrr_usd=row["mrr_usd"],
        expected_mrr_loss_usd=row["expected_mrr_loss_usd"], risk_rank=int(row["risk_rank"]),
        plan=row["plan"], industry=row["industry"], top_drivers=json.loads(row["top_drivers"]),
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["ops"])
def health() -> dict:
    status = {"status": "ok", "version": __version__}
    try:
        get_model()
        status["model_loaded"] = True
    except RuntimeError as exc:
        status.update(status="degraded", model_loaded=False, detail=str(exc))
    try:
        latest = query("select max(snapshot_month) as m from ml.churn_scores")["m"].iat[0]
        status["latest_scores"] = str(latest)
    except duckdb.Error:
        status.update(status="degraded", latest_scores=None)
    return status


@app.get("/v1/accounts/at-risk", response_model=list[AccountRisk], tags=["accounts"])
def at_risk_accounts(
    tier: Tier | None = Query(None, description="Filter by risk tier"),
    min_mrr: float = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=500),
) -> list[AccountRisk]:
    """Accounts ranked by expected MRR loss (probability x MRR) - the CS team's call list."""
    sql = _RISK_SQL + " where s.mrr_usd >= ?"
    params: list = [min_mrr]
    if tier:
        sql += " and s.risk_tier = ?"
        params.append(tier)
    sql += " order by s.expected_mrr_loss_usd desc limit ?"
    params.append(limit)
    return [_to_risk(r) for _, r in query(sql, params).iterrows()]


@app.get("/v1/accounts/{account_id}/churn-risk", response_model=AccountRisk, tags=["accounts"])
def account_risk(account_id: str) -> AccountRisk:
    df = query(_RISK_SQL + " where s.account_id = ?", [account_id])
    if df.empty:
        raise HTTPException(404, f"no active-account score for {account_id}")
    return _to_risk(df.iloc[0])


@app.post("/v1/score", response_model=ScoreResponse, tags=["scoring"])
def score(features: AccountFeatures) -> ScoreResponse:
    """Real-time scoring for a single account's feature vector."""
    X = prepare(pd.DataFrame([features.model_dump()]))
    p = float(get_model().predict_proba(X)[:, 1][0])
    return ScoreResponse(
        churn_probability=round(p, 4), risk_tier=risk_tier(p),
        expected_mrr_loss_usd=round(p * features.mrr_usd, 2), model_version=__version__,
    )


@app.get("/v1/metrics/summary", response_model=KpiSummary, tags=["metrics"])
def kpi_summary() -> KpiSummary:
    m = query("select * from marts.metrics_mrr_monthly order by month_start desc limit 1").iloc[0]
    r = query("""select count(*) filter (where risk_tier = 'high') as high,
                        coalesce(sum(expected_mrr_loss_usd), 0) as loss from ml.churn_scores""").iloc[0]
    return KpiSummary(
        month=m["month_start"], mrr_usd=m["ending_mrr_usd"], arr_usd=m["ending_arr_usd"],
        active_customers=int(m["ending_customers"]), logo_churn_rate=m["logo_churn_rate"],
        net_mrr_retention_rate=m["net_mrr_retention_rate"], accounts_high_risk=int(r["high"]),
        expected_mrr_loss_usd=round(float(r["loss"]), 2),
    )
