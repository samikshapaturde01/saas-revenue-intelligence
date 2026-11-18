"""End-to-end: simulate -> ingest -> dbt build -> train -> score -> serve."""

import json

import duckdb
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def con(pipeline_env):
    c = duckdb.connect(str(pipeline_env.warehouse_path), read_only=True)
    yield c
    c.close()


@pytest.fixture(scope="module")
def client(pipeline_env):
    from revintel.api.app import app, get_model

    get_model.cache_clear()
    return TestClient(app)


def test_warehouse_marts_populated(con):
    for table in ["marts.dim_accounts", "marts.fct_mrr_movements", "marts.metrics_mrr_monthly",
                  "marts.fct_cohort_retention", "marts.mart_churn_features", "ml.churn_scores"]:
        assert con.execute(f"select count(*) from {table}").fetchone()[0] > 0, table


def test_model_metrics_beat_random(pipeline_env):
    metrics = json.loads((pipeline_env.path("artifacts") / "model" / "metrics.json").read_text())
    assert metrics["validation"]["roc_auc"] > 0.6
    assert metrics["validation"]["lift_at_top_decile"] > 1.5


def test_scores_cover_all_active_accounts(con):
    active = con.execute("select count(*) from marts.dim_accounts where customer_status='active'").fetchone()[0]
    scored = con.execute("select count(*) from ml.churn_scores").fetchone()[0]
    assert active == scored


def test_api_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["model_loaded"]


def test_api_at_risk_and_account_lookup(client):
    risky = client.get("/v1/accounts/at-risk", params={"limit": 5}).json()
    assert 0 < len(risky) <= 5
    losses = [r["expected_mrr_loss_usd"] for r in risky]
    assert losses == sorted(losses, reverse=True)
    one = client.get(f"/v1/accounts/{risky[0]['account_id']}/churn-risk").json()
    assert one["account_id"] == risky[0]["account_id"]
    assert client.get("/v1/accounts/ACC-999999/churn-risk").status_code == 404


def test_api_realtime_scoring(client):
    healthy = {"plan": "enterprise", "tenure_months": 20, "seats": 80, "mrr_usd": 3600,
               "active_days": 26, "seat_utilization": 0.6, "sessions_per_seat": 30,
               "core_events_per_session": 4.0, "uses_integrations": True, "avg_csat_3m": 4.6}
    unhealthy = {**healthy, "plan": "starter", "tenure_months": 3, "seat_utilization": 0.05,
                 "sessions_per_seat": 2, "core_events_per_session": 1.2, "dau_trend_vs_prev_3m": 0.4,
                 "low_csat_tickets_3m": 3, "avg_csat_3m": 1.8, "failed_invoices_3m": 1}
    p_good = client.post("/v1/score", json=healthy).json()["churn_probability"]
    p_bad = client.post("/v1/score", json=unhealthy).json()["churn_probability"]
    assert p_bad > p_good
    assert client.post("/v1/score", json={**healthy, "seats": 0}).status_code == 422


def test_api_kpi_summary(client):
    body = client.get("/v1/metrics/summary").json()
    assert body["mrr_usd"] > 0 and body["active_customers"] > 0
