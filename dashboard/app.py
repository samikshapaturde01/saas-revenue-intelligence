"""
Customer Health Dashboard (Streamlit).

    streamlit run dashboard/app.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from revintel.config import get_settings  # noqa: E402

BLUE, ORANGE, AQUA, YELLOW, MAGENTA, RED = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#e34948"
GRID, MUTED = "#e1e0d9", "#898781"
TIER_COLORS = {"high": "#d03b3b", "medium": "#fab219", "low": "#0ca30c"}

st.set_page_config(page_title="Customer Health | Revenue Intelligence", layout="wide")


@st.cache_data(ttl=300)
def q(sql: str) -> pd.DataFrame:
    with duckdb.connect(str(get_settings().warehouse_path), read_only=True) as con:
        return con.execute(sql).df()


def layout(fig: go.Figure, height: int = 340) -> go.Figure:
    fig.update_layout(
        height=height, margin=dict(l=10, r=10, t=30, b=10), plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)", hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        font=dict(size=12),
    )
    fig.update_xaxes(showgrid=False, linecolor="#c3c2b7")
    fig.update_yaxes(gridcolor=GRID, zeroline=False)
    return fig


def fmt_money(x: float) -> str:
    return f"${x / 1e6:,.2f}M" if abs(x) >= 1e6 else f"${x / 1e3:,.1f}K"


# --------------------------------------------------------------------------- #
try:
    mrr = q("select * from marts.metrics_mrr_monthly order by month_start")
except duckdb.Error:
    st.error("Warehouse not found. Run `make pipeline` first.")
    st.stop()

mrr["month_start"] = pd.to_datetime(mrr["month_start"])
latest, prev = mrr.iloc[-1], mrr.iloc[-2]

st.title("Customer Health & Revenue Intelligence")
st.caption(f"Tasklane (fictional B2B SaaS) · data through {latest['month_start']:%B %Y}")

tab_overview, tab_retention, tab_risk, tab_data = st.tabs(
    ["Revenue overview", "Retention", "At-risk accounts", "Data health"]
)

# --------------------------------------------------------------------------- #
with tab_overview:
    c = st.columns(5)
    c[0].metric("MRR", fmt_money(latest["ending_mrr_usd"]),
                f"{latest['mrr_growth_rate']:+.1%} MoM")
    c[1].metric("ARR", fmt_money(latest["ending_arr_usd"]))
    c[2].metric("Active customers", f"{int(latest['ending_customers']):,}",
                f"{int(latest['ending_customers'] - prev['ending_customers']):+,}")
    c[3].metric("Logo churn (monthly)", f"{latest['logo_churn_rate']:.1%}",
                f"{(latest['logo_churn_rate'] - prev['logo_churn_rate']) * 100:+.1f} pp", delta_color="inverse")
    c[4].metric("Net MRR retention (monthly)", f"{latest['net_mrr_retention_rate']:.1%}")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("MRR growth")
        fig = go.Figure(go.Scatter(x=mrr["month_start"], y=mrr["ending_mrr_usd"], mode="lines",
                                   line=dict(color=BLUE, width=2), name="MRR",
                                   hovertemplate="%{y:$,.0f}<extra></extra>"))
        st.plotly_chart(layout(fig), use_container_width=True)
    with right:
        st.subheader(f"MRR bridge · {latest['month_start']:%b %Y}")
        steps = ["new_mrr_usd", "expansion_mrr_usd", "reactivation_mrr_usd",
                 "contraction_mrr_usd", "churned_mrr_usd"]
        fig = go.Figure(go.Waterfall(
            x=["Start", "New", "Expansion", "Reactivation", "Contraction", "Churn", "End"],
            measure=["absolute"] + ["relative"] * 5 + ["total"],
            y=[latest["starting_mrr_usd"], *[latest[s] for s in steps], 0],
            increasing=dict(marker_color=AQUA), decreasing=dict(marker_color=ORANGE),
            totals=dict(marker_color=BLUE), connector=dict(line=dict(color=GRID)),
            hovertemplate="%{y:$,.0f}<extra></extra>",
        ))
        fig.update_yaxes(range=[latest["starting_mrr_usd"] * 0.9, latest["ending_mrr_usd"] * 1.05])
        st.plotly_chart(layout(fig).update_layout(hovermode="closest"), use_container_width=True)

    st.subheader("Monthly MRR movements")
    fig = go.Figure()
    for col, name, color in [("new_mrr_usd", "New", BLUE), ("expansion_mrr_usd", "Expansion", AQUA),
                             ("reactivation_mrr_usd", "Reactivation", YELLOW),
                             ("contraction_mrr_usd", "Contraction", MAGENTA),
                             ("churned_mrr_usd", "Churn", ORANGE)]:
        fig.add_bar(x=mrr["month_start"], y=mrr[col], name=name, marker_color=color,
                    hovertemplate="%{y:$,.0f}")
    st.plotly_chart(layout(fig.update_layout(barmode="relative", bargap=0.25), 320),
                    use_container_width=True)

# --------------------------------------------------------------------------- #
with tab_retention:
    coh = q("select * from marts.fct_cohort_retention")
    coh["cohort_month"] = pd.to_datetime(coh["cohort_month"]).dt.strftime("%Y-%m")
    metric = st.radio("Metric", ["logo_retention", "net_revenue_retention"], horizontal=True,
                      format_func=lambda m: {"logo_retention": "Logo retention",
                                             "net_revenue_retention": "Net revenue retention"}[m])
    pivot = coh.pivot(index="cohort_month", columns="months_since_start", values=metric)
    fig = go.Figure(go.Heatmap(
        z=pivot.values, x=pivot.columns, y=pivot.index, zmin=0, zmax=1.2 if "revenue" in metric else 1,
        colorscale=[[0, "#f0efec"], [0.5, "#86b6ef"], [1, "#104281"]], xgap=2, ygap=2,
        hovertemplate="Cohort %{y}<br>Month %{x}: %{z:.0%}<extra></extra>",
    ))
    fig.update_xaxes(title="Months since first subscription")
    st.plotly_chart(layout(fig, 620).update_layout(hovermode="closest"), use_container_width=True)
    st.caption("The drop at month 12 is the annual-renewal cliff: annual customers can only churn at renewal.")

# --------------------------------------------------------------------------- #
with tab_risk:
    try:
        scores = q("""select s.*, a.company_name, a.industry, a.company_size, a.current_plan as plan
                      from ml.churn_scores s join marts.dim_accounts a using (account_id)""")
    except duckdb.Error:
        st.info("No scores yet. Run `revintel train && revintel score`.")
        st.stop()

    c = st.columns(4)
    c[0].metric("Accounts scored", f"{len(scores):,}")
    c[1].metric("High-risk accounts", f"{(scores['risk_tier'] == 'high').sum():,}")
    c[2].metric("Expected MRR loss (3 mo.)", fmt_money(scores["expected_mrr_loss_usd"].sum()))
    c[3].metric("MRR in high-risk accounts",
                fmt_money(scores.loc[scores["risk_tier"] == "high", "mrr_usd"].sum()))

    f1, f2, f3 = st.columns(3)
    tiers = f1.multiselect("Risk tier", ["high", "medium", "low"], default=["high", "medium"])
    plans = f2.multiselect("Plan", sorted(scores["plan"].dropna().unique()))
    min_mrr = f3.number_input("Min MRR ($)", 0, step=500)

    view = scores[scores["risk_tier"].isin(tiers) & (scores["mrr_usd"] >= min_mrr)]
    if plans:
        view = view[view["plan"].isin(plans)]
    view = view.sort_values("expected_mrr_loss_usd", ascending=False)
    view["top_risk_drivers"] = view["top_drivers"].map(
        lambda s: " · ".join(d["label"] for d in json.loads(s)) or "—")

    st.dataframe(
        view[["account_id", "company_name", "plan", "industry", "mrr_usd", "churn_probability",
              "risk_tier", "expected_mrr_loss_usd", "top_risk_drivers"]],
        use_container_width=True, hide_index=True, height=420,
        column_config={
            "mrr_usd": st.column_config.NumberColumn("MRR", format="$%.0f"),
            "churn_probability": st.column_config.ProgressColumn("Churn prob.", min_value=0, max_value=1,
                                                                 format="%.2f"),
            "expected_mrr_loss_usd": st.column_config.NumberColumn("Expected loss", format="$%.0f"),
        },
    )

    st.subheader("Account drill-down")
    acc = st.selectbox("Account", view["account_id"].head(200),
                       format_func=lambda a: f"{a} · {scores.set_index('account_id').at[a, 'company_name']}")
    if acc:
        row = scores.set_index("account_id").loc[acc]
        st.markdown(f"**{row['company_name']}** · {row['plan']} plan · {row['industry']} · "
                    f"churn probability **{row['churn_probability']:.0%}** ({row['risk_tier']} risk)")
        drivers = pd.DataFrame(json.loads(row["top_drivers"]))
        if drivers.empty:
            st.write("No strong risk drivers — account looks healthy.")
        else:
            drivers = drivers.rename(columns={"label": "Driver", "value": "This account",
                                              "typical_healthy_value": "Typical healthy account",
                                              "impact": "Risk reduction if fixed"})
            st.dataframe(drivers.drop(columns="feature"), hide_index=True, use_container_width=True)

# --------------------------------------------------------------------------- #
with tab_data:
    audit = q("""select source, count(*) as files, sum(rows_loaded) as rows_loaded,
                        sum(rows_quarantined) as rows_quarantined,
                        count(*) filter (where status = 'rejected') as files_rejected,
                        max(loaded_at) as last_loaded_at
                 from meta.load_audit group by 1 order by 1""")
    st.subheader("Ingestion audit")
    st.dataframe(audit, hide_index=True, use_container_width=True)
    st.subheader("Quarantined rows by reason")
    quarantine = q("""select source, reject_reason, count(*) as rows
                      from meta.quarantine group by 1, 2 order by 3 desc""")
    st.dataframe(quarantine, hide_index=True, use_container_width=True)
