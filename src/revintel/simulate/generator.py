"""
Synthetic B2B SaaS business simulator.

Simulates a fictional project-management SaaS company ("Tasklane") month by month
and writes the kind of raw exports you would get from its source systems:

    crm/accounts.csv                 full snapshot from the CRM
    billing/subscription_events_*.csv   change log from the billing system
    billing/invoices_*.csv           invoices in local currency
    product/usage_daily_*.parquet    daily product-usage rollups
    support/tickets_*.jsonl          helpdesk tickets

Churn is driven by a latent "account health" process, so the leading indicators
(falling usage, failed payments, unhappy support interactions, seat contraction)
show up in the data *before* an account cancels, exactly like in real life.

The exports also contain realistic data-quality problems (duplicates, inconsistent
casing, mixed time zones, invalid rows) that the ingestion layer and the dbt
staging models have to deal with.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Business constants
# --------------------------------------------------------------------------- #
PLANS = ["starter", "growth", "enterprise"]
PRICE_PER_SEAT_USD = np.array([12.0, 25.0, 45.0])
ANNUAL_DISCOUNT = 0.15

SIZES = ["1-10", "11-50", "51-200", "201-1000", "1000+"]
SIZE_P = [0.38, 0.30, 0.18, 0.10, 0.04]
SEAT_RANGE = [(2, 10), (6, 40), (20, 120), (50, 400), (150, 1200)]
PAYMENT_FAIL_P = np.array([0.07, 0.04, 0.025, 0.012, 0.006])

INDUSTRIES = ["Software", "Retail", "Healthcare", "Financial Services",
              "Education", "Manufacturing", "Media", "Professional Services"]
INDUSTRY_FIT = np.array([0.35, -0.15, 0.0, 0.1, -0.25, -0.1, 0.05, 0.2])

CHANNELS = ["organic", "paid_search", "partner", "outbound_sales", "referral"]
CHANNEL_P = [0.32, 0.25, 0.12, 0.16, 0.15]
CHANNEL_FIT = np.array([0.15, -0.35, 0.2, 0.0, 0.4])

COUNTRIES = ["US", "GB", "DE", "CA", "IN", "AU", "FR", "NL", "ES"]
COUNTRY_P = [0.45, 0.12, 0.10, 0.08, 0.08, 0.05, 0.05, 0.04, 0.03]
CURRENCY = {"GB": "GBP", "DE": "EUR", "FR": "EUR", "NL": "EUR", "ES": "EUR"}

TICKET_CATEGORIES = ["bug", "how_to", "billing", "feature_request", "integration", "performance"]

_SYL_A = ["Blue", "North", "Bright", "Iron", "Silver", "Clear", "Rapid", "Green", "Summit",
          "Pixel", "Harbor", "Atlas", "Nova", "Cedar", "Quantum", "Echo", "Delta", "Maple"]
_SYL_B = ["field", "stone", "wave", "path", "line", "works", "bridge", "point", "forge",
          "leaf", "peak", "grid", "loop", "craft", "spring", "gate", "mark", "logic"]
_SUFFIX = ["Inc", "Ltd", "LLC", "GmbH", "Labs", "Group", "Co", "Studio", "Partners", "Systems"]


@dataclass
class SimConfig:
    seed: int = 42
    start_date: str = "2024-01-01"
    n_months: int = 33
    base_signups_per_month: int = 70
    monthly_growth: float = 0.035


@dataclass
class _Outputs:
    events: list[dict] = field(default_factory=list)
    invoices: list[dict] = field(default_factory=list)
    tickets: list[dict] = field(default_factory=list)
    usage: list[pd.DataFrame] = field(default_factory=list)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _month_start(start: date, m: int) -> date:
    y, mo = divmod(start.month - 1 + m, 12)
    return date(start.year + y, mo + 1, 1)


def _days_in_month(d: date) -> int:
    nxt = _month_start(d, 1)
    return (nxt - d).days


class SaaSSimulator:
    """Month-by-month agent-style simulation, vectorised over accounts."""

    def __init__(self, cfg: SimConfig):
        self.cfg = cfg
        self.rng = np.random.default_rng(cfg.seed)
        self.start = date.fromisoformat(cfg.start_date)
        self.out = _Outputs()
        self._event_seq = 0
        self._invoice_seq = 0
        self._ticket_seq = 0
        self._sub_seq = 0

    # ------------------------------------------------------------------ #
    # Account universe
    # ------------------------------------------------------------------ #
    def _build_accounts(self) -> pd.DataFrame:
        rng, cfg = self.rng, self.cfg
        signups = [
            max(1, int(rng.poisson(cfg.base_signups_per_month * (1 + cfg.monthly_growth) ** m)))
            for m in range(cfg.n_months)
        ]
        n = int(sum(signups))
        signup_month = np.repeat(np.arange(cfg.n_months), signups)

        size_idx = rng.choice(len(SIZES), n, p=SIZE_P)
        ind_idx = rng.choice(len(INDUSTRIES), n)
        ch_idx = rng.choice(len(CHANNELS), n, p=CHANNEL_P)
        country = rng.choice(COUNTRIES, n, p=COUNTRY_P)

        signup_dates = []
        for m in signup_month:
            ms = _month_start(self.start, int(m))
            signup_dates.append(ms + timedelta(days=int(rng.integers(0, _days_in_month(ms)))))

        names = [
            f"{rng.choice(_SYL_A)}{rng.choice(_SYL_B)} {rng.choice(_SUFFIX)}" for _ in range(n)
        ]
        # product-market fit: how well this kind of customer suits the product
        fit = INDUSTRY_FIT[ind_idx] + CHANNEL_FIT[ch_idx] + 0.1 * size_idx + rng.normal(0, 0.6, n)

        return pd.DataFrame({
            "account_id": [f"ACC-{i:06d}" for i in range(1, n + 1)],
            "company_name": names,
            "industry": np.array(INDUSTRIES)[ind_idx],
            "company_size": np.array(SIZES)[size_idx],
            "size_idx": size_idx,
            "country": country,
            "acquisition_channel": np.array(CHANNELS)[ch_idx],
            "signup_month": signup_month,
            "signup_date": signup_dates,
            "fit": fit,
        })

    # ------------------------------------------------------------------ #
    # Event helpers
    # ------------------------------------------------------------------ #
    def _ts(self, d: date) -> datetime:
        secs = int(self.rng.integers(7 * 3600, 20 * 3600))
        return datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(seconds=secs)

    def _event(self, acc: str, sub: str, etype: str, d: date, plan: int, seats: int,
               cycle: str, discount: float, reason: str | None = None) -> None:
        self._event_seq += 1
        self.out.events.append({
            "event_id": f"EVT-{self._event_seq:08d}",
            "subscription_id": sub,
            "account_id": acc,
            "event_type": etype,
            "event_ts": self._ts(d).isoformat(),
            "plan": PLANS[plan],
            "seats": int(seats),
            "billing_cycle": cycle,
            "unit_price_usd": float(PRICE_PER_SEAT_USD[plan]),
            "discount_pct": round(float(discount), 2),
            "cancel_reason": reason,
        })

    def _new_sub_id(self) -> str:
        self._sub_seq += 1
        return f"SUB-{self._sub_seq:07d}"

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    def run(self) -> pd.DataFrame:
        rng = self.rng
        acc = self._build_accounts()
        n = len(acc)
        log.info("Simulating %s accounts over %s months", n, self.cfg.n_months)

        active = np.zeros(n, bool)
        ever = np.zeros(n, bool)
        churned_month = np.full(n, -99)
        churn_reason = np.array([None] * n, dtype=object)
        sub_id = np.array([None] * n, dtype=object)
        sub_start_m = np.zeros(n, int)
        anchor_day = np.ones(n, int)
        plan = np.zeros(n, int)
        seats = np.zeros(n, int)
        annual = np.zeros(n, bool)
        discount = np.zeros(n)
        health = np.zeros(n)
        failed_streak = np.zeros(n, int)
        low_csat = np.zeros(n, bool)
        last_failed = np.zeros(n, bool)

        ids = acc["account_id"].to_numpy()
        fit = acc["fit"].to_numpy()
        size_idx = acc["size_idx"].to_numpy()
        currency = np.array([CURRENCY.get(c, "USD") for c in acc["country"]])

        for m in range(self.cfg.n_months):
            ms = _month_start(self.start, m)
            dim = _days_in_month(ms)

            # ---------------- new signups ----------------
            new = np.where(acc["signup_month"].to_numpy() == m)[0]
            for i in new:
                lo, hi = SEAT_RANGE[size_idx[i]]
                seats[i] = int(rng.integers(lo, hi + 1))
                plan[i] = int(rng.choice(3, p=[[0.75, 0.23, 0.02], [0.45, 0.48, 0.07],
                                               [0.15, 0.6, 0.25], [0.05, 0.5, 0.45],
                                               [0.0, 0.3, 0.7]][size_idx[i]]))
                annual[i] = rng.random() < (0.2 + 0.12 * size_idx[i])
                discount[i] = rng.choice([0, 0, 0, 0.1, 0.2]) if plan[i] == 2 else 0.0
                health[i] = fit[i] + rng.normal(0.2, 0.5)
                sub_id[i] = self._new_sub_id()
                sub_start_m[i] = m
                d = acc.at[i, "signup_date"]
                anchor_day[i] = d.day
                active[i] = ever[i] = True
                self._event(ids[i], sub_id[i], "created", d, plan[i], seats[i],
                            "annual" if annual[i] else "monthly", discount[i])

            existing = np.where(active & (acc["signup_month"].to_numpy() < m))[0]

            # ---------------- health dynamics ----------------
            k = len(existing)
            if k:
                shock = (rng.random(k) < 0.025) * rng.normal(-1.6, 0.4, k)  # champion leaves etc.
                health[existing] = (0.85 * health[existing] + 0.15 * fit[existing]
                                    + rng.normal(0, 0.28, k) + shock - 0.01)

            # ---------------- expansion / contraction / plan changes ----------------
            for i in existing:
                h = health[i]
                changed = None
                r = rng.random()
                if r < _sigmoid(np.array(-3.2 + 1.1 * h)):
                    seats[i] += max(1, int(seats[i] * rng.uniform(0.08, 0.35)))
                    changed = "seats_changed"
                elif r > 1 - _sigmoid(np.array(-3.4 - 1.2 * h)) and seats[i] > 2:
                    seats[i] = max(2, int(seats[i] * rng.uniform(0.6, 0.9)))
                    changed = "seats_changed"
                if plan[i] < 2 and (seats[i] >= (20, 100)[plan[i]] or h > 1.4) and rng.random() < 0.07:
                    plan[i] += 1
                    changed = "plan_changed"
                elif plan[i] > 0 and h < -1.0 and rng.random() < 0.05:
                    plan[i] -= 1
                    changed = "plan_changed"
                if changed:
                    d = ms + timedelta(days=int(rng.integers(0, dim)))
                    self._event(ids[i], sub_id[i], changed, d, plan[i], seats[i],
                                "annual" if annual[i] else "monthly", discount[i])

            # ---------------- invoicing ----------------
            billable = np.where(active)[0]
            for i in billable:
                tenure = m - sub_start_m[i]
                if annual[i] and tenure % 12 != 0:
                    continue
                mrr = seats[i] * PRICE_PER_SEAT_USD[plan[i]] * (1 - discount[i])
                mrr *= (1 - ANNUAL_DISCOUNT) if annual[i] else 1
                amount_usd = mrr * (12 if annual[i] else 1)
                fx = {"USD": 1.0, "EUR": 0.92, "GBP": 0.79}[currency[i]]
                issue = ms + timedelta(days=int(min(anchor_day[i], dim)) - 1)
                p_fail = PAYMENT_FAIL_P[size_idx[i]] + (0.04 if health[i] < -0.8 else 0)
                failed = rng.random() < p_fail
                recovered = failed and rng.random() < 0.55
                status = "paid" if (not failed or recovered) else "failed"
                attempts = 1 if not failed else int(rng.integers(2, 5))
                paid_at = None
                if status == "paid":
                    paid_at = self._ts(issue + timedelta(days=int(rng.integers(0, 3)) + (5 if recovered else 0)))
                self._invoice_seq += 1
                self.out.invoices.append({
                    "invoice_id": f"INV-{self._invoice_seq:08d}",
                    "account_id": ids[i],
                    "subscription_id": sub_id[i],
                    "issued_at": self._ts(issue).isoformat(),
                    "due_at": (issue + timedelta(days=14)).isoformat(),
                    "currency": currency[i],
                    "amount_cents": int(round(amount_usd * fx * 100)),
                    "status": status,
                    "attempt_count": attempts,
                    "paid_at": paid_at.isoformat() if paid_at else None,
                })
                last_failed[i] = status == "failed"
                failed_streak[i] = failed_streak[i] + 1 if status == "failed" else 0

            # ---------------- product usage (daily) ----------------
            idx = np.where(active)[0]
            if len(idx):
                start_day = np.where(acc["signup_month"].to_numpy()[idx] == m,
                                     np.array([acc.at[i, "signup_date"].day for i in idx]), 1)
                days = np.arange(1, dim + 1)
                dates = np.array([ms + timedelta(days=int(d) - 1) for d in days])
                weekday = np.array([dt.weekday() < 5 for dt in dates])
                util = _sigmoid(0.9 * health[idx] + 0.2)[:, None]
                day_factor = np.where(weekday, 1.0, 0.18)[None, :]
                p = np.clip(util * day_factor * rng.uniform(0.85, 1.05, (len(idx), dim)), 0, 1)
                dau = rng.binomial(np.repeat(seats[idx][:, None], dim, axis=1), p)
                sessions = rng.poisson(dau * 2.4)
                core = rng.poisson(sessions * 3.1 * np.clip(1 + 0.25 * health[idx], 0.2, None)[:, None])
                integr = rng.poisson(sessions * 0.4 * (plan[idx] >= 1)[:, None])
                mask = days[None, :] >= start_day[:, None]
                rows, cols = np.where(mask & (dau > 0))
                self.out.usage.append(pd.DataFrame({
                    "account_id": ids[idx][rows],
                    "activity_date": dates[cols],
                    "active_users": dau[rows, cols],
                    "sessions": sessions[rows, cols],
                    "core_feature_events": core[rows, cols],
                    "integration_events": integr[rows, cols],
                }))

            # ---------------- support tickets ----------------
            low_csat[:] = False
            for i in idx:
                lam = 0.12 + 0.025 * np.sqrt(seats[i]) + (0.35 if health[i] < -0.6 else 0) \
                    + (0.5 if last_failed[i] else 0)
                for _ in range(rng.poisson(lam)):
                    cat = "billing" if last_failed[i] and rng.random() < 0.6 else \
                        rng.choice(TICKET_CATEGORIES, p=[0.3, 0.28, 0.07, 0.15, 0.12, 0.08])
                    prio = rng.choice(["low", "normal", "high", "urgent"], p=[0.3, 0.45, 0.2, 0.05])
                    created = self._ts(ms + timedelta(days=int(rng.integers(0, dim))))
                    frt = float(np.round(rng.lognormal(4.2 - 0.4 * (prio in ("high", "urgent")), 0.8), 1))
                    resolved = created + timedelta(hours=float(rng.lognormal(3.0, 1.0)))
                    csat = int(np.clip(np.round(3.6 + 0.8 * health[i] - 0.004 * frt
                                                + rng.normal(0, 0.9)), 1, 5))
                    if csat <= 2:
                        low_csat[i] = True
                    self._ticket_seq += 1
                    self.out.tickets.append({
                        "ticket_id": f"TCK-{self._ticket_seq:07d}",
                        "account_id": ids[i],
                        "created_at": created,
                        "priority": prio,
                        "category": cat,
                        "first_response_minutes": frt,
                        "resolved_at": resolved if rng.random() > 0.04 else None,
                        "csat_score": csat if rng.random() > 0.38 else None,
                    })

            # ---------------- churn ----------------
            for i in existing:
                tenure = m - sub_start_m[i]
                if failed_streak[i] >= 2 or (failed_streak[i] == 1 and rng.random() < 0.3):
                    reason = "payment_failed"
                else:
                    logit = (-3.3 - 1.25 * health[i] + 0.8 * last_failed[i] + 0.7 * low_csat[i]
                             - 0.35 * (plan[i] == 2) + 0.15 * (seats[i] < 5))
                    if annual[i]:
                        if tenure % 12 != 11:
                            continue
                        logit += 1.6
                    if rng.random() >= _sigmoid(np.array(logit)):
                        continue
                    reason = rng.choice(
                        ["low_usage", "switched_competitor", "too_expensive",
                         "missing_features", "business_closed"],
                        p=[0.38, 0.2, 0.2, 0.14, 0.08] if health[i] < 0 else [0.12, 0.2, 0.33, 0.2, 0.15])
                d = ms + timedelta(days=int(rng.integers(max(0, dim - 10), dim)))
                self._event(ids[i], sub_id[i], "cancelled", d, plan[i], seats[i],
                            "annual" if annual[i] else "monthly", discount[i], reason)
                active[i] = False
                churned_month[i] = m
                churn_reason[i] = reason

            # ---------------- win-backs ----------------
            gone = np.where(~active & ever & (churned_month <= m - 2)
                            & (churn_reason != "business_closed"))[0]
            for i in gone[rng.random(len(gone)) < 0.012]:
                plan[i] = max(0, plan[i] - int(rng.random() < 0.4))
                seats[i] = max(2, int(seats[i] * rng.uniform(0.5, 1.0)))
                annual[i] = False
                health[i] = fit[i] + rng.normal(0.3, 0.4)
                failed_streak[i] = 0
                sub_id[i] = self._new_sub_id()
                sub_start_m[i] = m + 1
                active[i] = True
                d = _month_start(self.start, m + 1)
                anchor_day[i] = 1
                if m + 1 < self.cfg.n_months:
                    self._event(ids[i], sub_id[i], "reactivated", d, plan[i], seats[i],
                                "monthly", discount[i])
                else:
                    active[i] = False

        return acc


# --------------------------------------------------------------------------- #
# Writing raw exports (with deliberate, realistic data-quality issues)
# --------------------------------------------------------------------------- #
def _month_key(ts: pd.Series) -> pd.Series:
    return pd.to_datetime(ts, utc=True, format="mixed").dt.strftime("%Y-%m")


def write_exports(sim: SaaSSimulator, accounts: pd.DataFrame, landing: Path) -> dict[str, int]:
    rng = np.random.default_rng(sim.cfg.seed + 1)
    if landing.exists():
        shutil.rmtree(landing)
    counts: dict[str, int] = {}
    last_month = _month_start(sim.start, sim.cfg.n_months - 1)
    extract_ts = datetime.combine(_month_start(last_month, 1), datetime.min.time())

    # ---- CRM snapshot: duplicates with stale versions, messy country codes, missing industry
    crm = accounts.drop(columns=["size_idx", "signup_month", "fit"]).copy()
    crm["created_at"] = pd.to_datetime(crm.pop("signup_date"))
    crm["updated_at"] = crm["created_at"] + pd.to_timedelta(rng.integers(0, 400, len(crm)), unit="D")
    crm["updated_at"] = crm["updated_at"].clip(upper=pd.Timestamp(extract_ts))
    dup = crm.sample(frac=0.03, random_state=1).copy()
    dup["updated_at"] = dup["updated_at"] - pd.Timedelta(days=30)
    dup["company_size"] = rng.choice(SIZES, len(dup))
    crm = pd.concat([crm, dup]).sample(frac=1, random_state=2)
    messy = rng.random(len(crm))
    crm["country"] = np.where(messy < 0.05, crm["country"].str.lower(),
                              np.where(messy < 0.08, " " + crm["country"] + " ", crm["country"]))
    crm.loc[rng.random(len(crm)) < 0.012, "industry"] = None
    out = landing / "crm" / f"extract_date={extract_ts:%Y-%m-%d}"
    out.mkdir(parents=True)
    crm.to_csv(out / "accounts.csv", index=False)
    counts["crm.accounts"] = len(crm)

    # ---- Billing: monthly extracts
    events = pd.DataFrame(sim.out.events)
    invoices = pd.DataFrame(sim.out.invoices)
    bad = invoices.sample(frac=0.001, random_state=3).index
    invoices.loc[bad, "amount_cents"] = -invoices.loc[bad, "amount_cents"]  # bad upstream rows
    for name, df, col in [("subscription_events", events, "event_ts"), ("invoices", invoices, "issued_at")]:
        for mk, part in df.groupby(_month_key(df[col])):
            d = landing / "billing" / f"extract_date={mk}-01"
            d.mkdir(parents=True, exist_ok=True)
            part.to_csv(d / f"{name}_{mk}.csv", index=False)
        counts[f"billing.{name}"] = len(df)

    # ---- Product usage: parquet per month with re-sent duplicate rows
    usage = pd.concat(sim.out.usage, ignore_index=True)
    usage = pd.concat([usage, usage.sample(frac=0.004, random_state=4)])
    usage["activity_date"] = pd.to_datetime(usage["activity_date"])
    for mk, part in usage.groupby(usage["activity_date"].dt.strftime("%Y-%m")):
        d = landing / "product" / f"extract_date={mk}-01"
        d.mkdir(parents=True, exist_ok=True)
        part.to_parquet(d / f"usage_daily_{mk}.parquet", index=False)
    counts["product.usage_daily"] = len(usage)

    # ---- Support: JSON lines, mixed time zones, inconsistent casing, orphan rows
    tickets = pd.DataFrame(sim.out.tickets)
    tz_choice = rng.choice([0, -5, 1, 5.5], len(tickets), p=[0.55, 0.25, 0.12, 0.08])
    case_choice = rng.random(len(tickets))
    for mk, part in tickets.groupby(tickets["created_at"].map(lambda t: t.strftime("%Y-%m"))):
        d = landing / "support" / f"extract_date={mk}-01"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"tickets_{mk}.jsonl", "w") as fh:
            for i, row in part.iterrows():
                tz = timezone(timedelta(hours=float(tz_choice[i])))
                rec = row.to_dict()
                rec["created_at"] = rec["created_at"].astimezone(tz).isoformat()
                rec["resolved_at"] = rec["resolved_at"].astimezone(tz).isoformat() \
                    if rec["resolved_at"] is not None and not pd.isna(rec["resolved_at"]) else None
                if case_choice[i] < 0.1:
                    rec["priority"] = rec["priority"].upper()
                elif case_choice[i] < 0.2:
                    rec["priority"] = rec["priority"].title()
                if case_choice[i] > 0.998:
                    rec["account_id"] = None
                if rec["csat_score"] is not None and not pd.isna(rec["csat_score"]):
                    rec["csat_score"] = int(rec["csat_score"])
                else:
                    rec["csat_score"] = None
                fh.write(json.dumps(rec) + "\n")
    counts["support.tickets"] = len(tickets)
    return counts


def generate(cfg: SimConfig, landing: Path) -> dict[str, int]:
    sim = SaaSSimulator(cfg)
    accounts = sim.run()
    counts = write_exports(sim, accounts, landing)
    for k, v in counts.items():
        log.info("wrote %-30s %10s rows", k, f"{v:,}")
    return counts
