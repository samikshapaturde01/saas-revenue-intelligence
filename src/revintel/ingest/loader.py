"""
Incremental, idempotent loader: landing zone files -> DuckDB `raw` schema.

* Each file is fingerprinted (sha256). Files already loaded successfully are skipped,
  so re-running the pipeline is safe and only picks up new extracts.
* Rows are validated against config/contracts.yaml; bad rows go to `meta.quarantine`.
* Every attempt is recorded in `meta.load_audit` for observability.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import yaml

from revintel.ingest.quality import ContractViolation, validate

log = logging.getLogger(__name__)

AUDIT_DDL = """
create schema if not exists raw;
create schema if not exists meta;
create table if not exists meta.load_audit (
    batch_id          varchar,
    source            varchar,
    file_path         varchar,
    file_hash         varchar,
    status            varchar,      -- loaded | rejected | failed
    rows_read         bigint,
    rows_loaded       bigint,
    rows_quarantined  bigint,
    message           varchar,
    duration_ms       bigint,
    loaded_at         timestamptz
);
create table if not exists meta.quarantine (
    batch_id      varchar,
    source        varchar,
    file_path     varchar,
    reject_reason varchar,
    payload       json,
    quarantined_at timestamptz
);
"""


@dataclass
class LoadSummary:
    files_seen: int = 0
    files_loaded: int = 0
    files_skipped: int = 0
    files_rejected: int = 0
    rows_loaded: int = 0
    rows_quarantined: int = 0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read(path: Path, fmt: str) -> pd.DataFrame:
    if fmt == "csv":
        return pd.read_csv(path, dtype=str, keep_default_na=True)
    if fmt == "jsonl":
        df = pd.read_json(path, lines=True, dtype=False, convert_dates=False)
        return df.astype(object).where(df.notna(), None)
    if fmt == "parquet":
        return pd.read_parquet(path)
    raise ValueError(f"unsupported format {fmt}")


def _as_raw(df: pd.DataFrame, columns: dict[str, dict], fmt: str) -> pd.DataFrame:
    """Raw layer keeps values as delivered: text files stay text (typed later in dbt
    staging); typed formats like parquet keep their native numeric types."""
    out = df.copy()
    for c, rule in columns.items():
        if fmt != "parquet" or rule.get("type") in {"string", "timestamp"}:
            out[c] = out[c].astype("string")
    return out


class Loader:
    def __init__(self, con: duckdb.DuckDBPyConnection, landing: Path, contracts_path: Path):
        self.con = con
        self.landing = landing
        with open(contracts_path) as fh:
            self.contracts = yaml.safe_load(fh)
        self.con.execute(AUDIT_DDL)

    def _already_loaded(self, source: str, file_hash: str) -> bool:
        row = self.con.execute(
            "select 1 from meta.load_audit where source = ? and file_hash = ? and status = 'loaded' limit 1",
            [source, file_hash],
        ).fetchone()
        return row is not None

    def _audit(self, **kw: Any) -> None:
        kw.setdefault("loaded_at", datetime.now(UTC))
        cols = ", ".join(kw)
        self.con.execute(
            f"insert into meta.load_audit ({cols}) values ({', '.join('?' * len(kw))})",
            list(kw.values()),
        )

    def _write_raw(self, source: str, df: pd.DataFrame) -> None:
        table = f"raw.{source}"
        self.con.register("_incoming", df)
        exists = self.con.execute(
            "select count(*) from information_schema.tables where table_schema='raw' and table_name=?",
            [source],
        ).fetchone()[0]
        if exists:
            self.con.execute(f"insert into {table} by name select * from _incoming")
        else:
            self.con.execute(f"create table {table} as select * from _incoming")
        self.con.unregister("_incoming")

    def _quarantine(self, batch_id: str, source: str, file: str, rejected: pd.DataFrame) -> None:
        if rejected.empty:
            return
        q = pd.DataFrame({
            "batch_id": batch_id,
            "source": source,
            "file_path": file,
            "reject_reason": rejected["_reject_reason"].to_numpy(),
            "payload": [
                json.dumps(r, default=str)
                for r in rejected.drop(columns="_reject_reason").to_dict(orient="records")
            ],
            "quarantined_at": datetime.now(UTC),
        })
        self.con.register("_q", q)
        self.con.execute("insert into meta.quarantine select * from _q")
        self.con.unregister("_q")

    def load_source(self, source: str, batch_id: str, summary: LoadSummary) -> None:
        contract = self.contracts["sources"][source]
        max_reject = contract.get("max_reject_rate", self.contracts["defaults"]["max_reject_rate"])
        files = sorted(self.landing.glob(contract["pattern"]))
        for path in files:
            summary.files_seen += 1
            rel = str(path.relative_to(self.landing))
            file_hash = _sha256(path)
            if self._already_loaded(source, file_hash):
                summary.files_skipped += 1
                continue
            t0 = time.perf_counter()
            df = _read(path, contract["format"])
            try:
                result = validate(df, contract, max_reject)
            except ContractViolation as exc:
                log.warning("REJECTED %s: %s", rel, exc)
                summary.files_rejected += 1
                self._audit(batch_id=batch_id, source=source, file_path=rel, file_hash=file_hash,
                            status="rejected", rows_read=len(df), rows_loaded=0, rows_quarantined=0,
                            message=str(exc), duration_ms=int((time.perf_counter() - t0) * 1000))
                continue

            raw = _as_raw(result.valid, contract["columns"], contract["format"])
            raw["_source_file"] = rel
            raw["_file_hash"] = file_hash
            raw["_batch_id"] = batch_id
            raw["_loaded_at"] = datetime.now(UTC)

            self.con.execute("begin transaction")
            try:
                self._write_raw(source, raw)
                self._quarantine(batch_id, source, rel, result.rejected)
                self._audit(batch_id=batch_id, source=source, file_path=rel, file_hash=file_hash,
                            status="loaded", rows_read=len(df), rows_loaded=len(raw),
                            rows_quarantined=len(result.rejected),
                            message="; ".join(result.warnings) or None,
                            duration_ms=int((time.perf_counter() - t0) * 1000))
                self.con.execute("commit")
            except Exception:
                self.con.execute("rollback")
                raise
            summary.files_loaded += 1
            summary.rows_loaded += len(raw)
            summary.rows_quarantined += len(result.rejected)

    def run(self) -> LoadSummary:
        batch_id = datetime.now(UTC).strftime("b%Y%m%dT%H%M%S%f")
        summary = LoadSummary()
        for source in self.contracts["sources"]:
            self.load_source(source, batch_id, summary)
        log.info(
            "ingest batch %s: %d files loaded, %d skipped (already loaded), %d rejected | "
            "%s rows loaded, %s quarantined",
            batch_id, summary.files_loaded, summary.files_skipped, summary.files_rejected,
            f"{summary.rows_loaded:,}", f"{summary.rows_quarantined:,}",
        )
        return summary


def run_ingest(warehouse: Path, landing: Path, contracts: Path) -> LoadSummary:
    warehouse.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(warehouse)) as con:
        return Loader(con, landing, contracts).run()
