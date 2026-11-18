import duckdb
import yaml

from revintel.ingest.loader import Loader

CONTRACTS = {
    "defaults": {"max_reject_rate": 0.5},
    "sources": {
        "orders": {
            "pattern": "shop/extract_date=*/orders_*.csv",
            "format": "csv",
            "columns": {
                "order_id": {"type": "string", "required": True},
                "amount": {"type": "integer", "required": True, "min": 0},
            },
        }
    },
}


def _setup(tmp_path):
    (tmp_path / "contracts.yaml").write_text(yaml.safe_dump(CONTRACTS))
    d = tmp_path / "landing" / "shop" / "extract_date=2025-01-01"
    d.mkdir(parents=True)
    (d / "orders_1.csv").write_text("order_id,amount\nO1,10\nO2,-3\nO3,7\n")
    return tmp_path / "landing", tmp_path / "contracts.yaml"


def test_load_quarantine_and_idempotency(tmp_path):
    landing, contracts = _setup(tmp_path)
    con = duckdb.connect(str(tmp_path / "wh.duckdb"))

    first = Loader(con, landing, contracts).run()
    assert (first.files_loaded, first.rows_loaded, first.rows_quarantined) == (1, 2, 1)
    assert con.execute("select count(*) from raw.orders").fetchone()[0] == 2
    reason = con.execute("select reject_reason from meta.quarantine").fetchone()[0]
    assert reason == "amount:below_min"

    # re-running must not duplicate data
    second = Loader(con, landing, contracts).run()
    assert second.files_skipped == 1 and second.files_loaded == 0
    assert con.execute("select count(*) from raw.orders").fetchone()[0] == 2

    # a new extract is picked up incrementally
    new = landing / "shop" / "extract_date=2025-02-01"
    new.mkdir()
    (new / "orders_2.csv").write_text("order_id,amount\nO4,5\n")
    third = Loader(con, landing, contracts).run()
    assert third.files_loaded == 1
    assert con.execute("select count(*) from raw.orders").fetchone()[0] == 3


def test_bad_file_is_rejected_and_audited(tmp_path):
    landing, contracts = _setup(tmp_path)
    bad = landing / "shop" / "extract_date=2025-03-01"
    bad.mkdir()
    (bad / "orders_3.csv").write_text("order_id\nO9\n")  # missing column
    con = duckdb.connect(str(tmp_path / "wh.duckdb"))
    summary = Loader(con, landing, contracts).run()
    assert summary.files_rejected == 1
    status = con.execute(
        "select status from meta.load_audit where file_path like '%orders_3%'").fetchone()[0]
    assert status == "rejected"
