"""Shared fixtures. The `pipeline_env` fixture runs the full pipeline once, at small scale,
into a temporary directory so integration tests exercise the real code paths quickly."""

from __future__ import annotations

import os

import pytest

from revintel.config import get_settings

SMALL_RUN = {
    "REVINTEL_SIMULATION__BASE_SIGNUPS_PER_MONTH": "30",
    "REVINTEL_SIMULATION__N_MONTHS": "20",
}


@pytest.fixture(scope="session")
def pipeline_env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("revintel")
    env = {
        **SMALL_RUN,
        "REVINTEL_PATHS__LANDING": str(tmp / "landing"),
        "REVINTEL_PATHS__ARTIFACTS": str(tmp / "artifacts"),
        "REVINTEL_DB_PATH": str(tmp / "warehouse.duckdb"),
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    get_settings.cache_clear()

    from revintel.pipeline import main

    main(["all"])
    yield get_settings()

    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
