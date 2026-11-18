"""
Pipeline entrypoint / orchestrator.

    revintel generate    # simulate source systems -> data/landing
    revintel ingest      # landing -> raw (validated, incremental)
    revintel transform   # dbt build (models + tests)
    revintel train       # train & evaluate churn model
    revintel score       # batch-score active accounts -> ml.churn_scores
    revintel all         # everything above, in order
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import duckdb

from revintel.config import PROJECT_ROOT, get_settings

log = logging.getLogger("revintel")
DBT_DIR = PROJECT_ROOT / "dbt"


def step_generate() -> None:
    from revintel.simulate.generator import SimConfig, generate

    s = get_settings()
    generate(SimConfig(**s.simulation), s.path("landing"))


def step_ingest() -> None:
    from revintel.ingest.loader import run_ingest

    s = get_settings()
    summary = run_ingest(s.warehouse_path, s.path("landing"), s.path("contracts"))
    if summary.files_rejected:
        log.warning("%d file(s) rejected by data contracts - see meta.load_audit", summary.files_rejected)


def step_transform() -> None:
    s = get_settings()
    env = {**os.environ, "REVINTEL_DB_PATH": str(s.warehouse_path),
           "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
    cmd = ["dbt", "build", "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR),
           "--vars", json.dumps({"churn_horizon_months": s.model["horizon_months"]})]
    log.info("running: %s", " ".join(cmd))
    res = subprocess.run(cmd, env=env)
    if res.returncode != 0:
        raise SystemExit("dbt build failed")


def step_train() -> None:
    from revintel.ml.features import load_feature_table
    from revintel.ml.train import save_artifacts, save_charts, train

    s = get_settings()
    with duckdb.connect(str(s.warehouse_path), read_only=True) as con:
        df = load_feature_table(con)
    result = train(df, s.model)
    save_artifacts(result, s.path("artifacts"))
    save_charts(result, s.path("artifacts") / "charts")
    log.info("validation metrics: %s", json.dumps(result.metrics["validation"]))


def step_score() -> None:
    from revintel.ml.score import score_latest

    s = get_settings()
    with duckdb.connect(str(s.warehouse_path)) as con:
        score_latest(con, s.path("artifacts") / "model" / "churn_model.joblib")


STEPS = {
    "generate": step_generate,
    "ingest": step_ingest,
    "transform": step_transform,
    "train": step_train,
    "score": step_score,
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="revintel", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=[*STEPS, "all"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                        datefmt="%H:%M:%S")
    steps = list(STEPS) if args.step == "all" else [args.step]
    for name in steps:
        t0 = time.perf_counter()
        log.info("==> %s", name)
        STEPS[name]()
        log.info("<== %s finished in %.1fs", name, time.perf_counter() - t0)


if __name__ == "__main__":
    sys.exit(main())
