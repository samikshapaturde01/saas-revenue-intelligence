import pandas as pd

from revintel.simulate.generator import SaaSSimulator, SimConfig, write_exports

CFG = SimConfig(seed=1, n_months=8, base_signups_per_month=15)


def test_simulation_is_deterministic():
    a, b = SaaSSimulator(CFG), SaaSSimulator(CFG)
    a.run(), b.run()
    assert a.out.events == b.out.events
    assert len(a.out.invoices) == len(b.out.invoices)


def test_subscription_lifecycle_is_consistent():
    sim = SaaSSimulator(CFG)
    sim.run()
    ev = pd.DataFrame(sim.out.events)
    # every subscription starts with exactly one created/reactivated event
    starts = ev[ev.event_type.isin(["created", "reactivated"])].groupby("subscription_id").size()
    assert (starts == 1).all()
    # nothing happens to a subscription after it is cancelled
    for _, g in ev.sort_values("event_ts").groupby("subscription_id"):
        types = g.event_type.tolist()
        if "cancelled" in types:
            assert types[-1] == "cancelled" and types.count("cancelled") == 1


def test_exports_written(tmp_path):
    sim = SaaSSimulator(CFG)
    accounts = sim.run()
    counts = write_exports(sim, accounts, tmp_path)
    assert counts["crm.accounts"] >= len(accounts)  # includes injected duplicates
    assert list(tmp_path.glob("product/*/usage_daily_*.parquet"))
    assert list(tmp_path.glob("support/*/tickets_*.jsonl"))
