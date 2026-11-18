"""Configuration loading with environment-variable overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "settings.yaml"
ENV_PREFIX = "REVINTEL_"


def _coerce(value: str, current: Any) -> Any:
    if isinstance(current, bool):
        return value.lower() in {"1", "true", "yes"}
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    return value


def _apply_env_overrides(cfg: dict[str, Any]) -> dict[str, Any]:
    """Override nested keys with REVINTEL_<SECTION>__<KEY>=value."""
    for key, value in os.environ.items():
        if not key.startswith(ENV_PREFIX) or "__" not in key:
            continue
        section, _, field = key[len(ENV_PREFIX):].lower().partition("__")
        if section in cfg and field in cfg[section]:
            cfg[section][field] = _coerce(value, cfg[section][field])
    return cfg


@dataclass(frozen=True)
class Settings:
    raw: dict[str, Any]

    @property
    def simulation(self) -> dict[str, Any]:
        return self.raw["simulation"]

    @property
    def model(self) -> dict[str, Any]:
        return self.raw["model"]

    def path(self, name: str) -> Path:
        p = Path(self.raw["paths"][name])
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def warehouse_path(self) -> Path:
        env = os.environ.get("REVINTEL_DB_PATH")
        return Path(env) if env else self.path("warehouse")


@lru_cache(maxsize=1)
def get_settings(config_path: str | None = None) -> Settings:
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    with open(path) as fh:
        cfg = yaml.safe_load(fh)
    return Settings(_apply_env_overrides(cfg))
