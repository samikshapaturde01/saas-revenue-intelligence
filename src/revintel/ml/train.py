"""
Train and evaluate the churn model.

Validation design
-----------------
* Out-of-time split: the most recent `validation_months` labelled snapshot months are held out.
* Purge gap: training snapshots whose label window overlaps the validation period are dropped,
  so no future information leaks into training.
* A regularised logistic regression is the baseline; a gradient-boosted tree model is the
  challenger. The winner (by PR-AUC, which matters for an imbalanced target) is calibrated
  and refit on all labelled data for production scoring.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder, StandardScaler
from sklearn.impute import SimpleImputer

from revintel.ml.features import (
    BOOLEAN_FEATURES,
    CATEGORICAL_FEATURES,
    FEATURE_LABELS,
    FEATURES,
    NUMERIC_FEATURES,
    TARGET,
    prepare,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Model definitions
# --------------------------------------------------------------------------- #
def make_baseline(random_state: int) -> Pipeline:
    pre = ColumnTransformer([
        ("num", Pipeline([("impute", SimpleImputer(strategy="median", add_indicator=True)),
                          ("scale", StandardScaler())]), NUMERIC_FEATURES + BOOLEAN_FEATURES),
        ("cat", OneHotEncoder(handle_unknown="ignore", min_frequency=20), CATEGORICAL_FEATURES),
    ])
    clf = LogisticRegression(C=0.5, max_iter=2000, class_weight="balanced", random_state=random_state)
    return Pipeline([("pre", pre), ("clf", clf)])


def make_gbm(random_state: int) -> Pipeline:
    pre = ColumnTransformer(
        [
            ("num", "passthrough", NUMERIC_FEATURES + BOOLEAN_FEATURES),
            ("cat", OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1,
                                   encoded_missing_value=-1), CATEGORICAL_FEATURES),
        ],
        verbose_feature_names_out=False,
    )
    n_num = len(NUMERIC_FEATURES) + len(BOOLEAN_FEATURES)
    cat_mask = [False] * n_num + [True] * len(CATEGORICAL_FEATURES)
    clf = HistGradientBoostingClassifier(
        learning_rate=0.05,
        max_iter=400,
        max_leaf_nodes=24,
        min_samples_leaf=60,
        l2_regularization=1.0,
        categorical_features=cat_mask,
        early_stopping=True,
        validation_fraction=0.15,
        n_iter_no_change=30,
        random_state=random_state,
    )
    return Pipeline([("pre", pre), ("clf", clf)])


# --------------------------------------------------------------------------- #
# Evaluation helpers
# --------------------------------------------------------------------------- #
def lift_table(y: np.ndarray, p: np.ndarray, mrr: np.ndarray, n_bins: int = 10) -> pd.DataFrame:
    df = pd.DataFrame({"y": y, "p": p, "mrr": mrr})
    df["decile"] = pd.qcut(df["p"].rank(method="first", ascending=False), n_bins, labels=False) + 1
    base = df["y"].mean()
    out = df.groupby("decile").agg(
        accounts=("y", "size"), churners=("y", "sum"), avg_score=("p", "mean"),
        churned_mrr=("mrr", lambda s: s[df.loc[s.index, "y"] == 1].sum()),
    )
    out["churn_rate"] = out["churners"] / out["accounts"]
    out["lift"] = out["churn_rate"] / base
    out["cum_churner_capture"] = out["churners"].cumsum() / out["churners"].sum()
    out["cum_churned_mrr_capture"] = out["churned_mrr"].cumsum() / out["churned_mrr"].sum()
    return out.reset_index()


def evaluate(y: np.ndarray, p: np.ndarray, mrr: np.ndarray) -> dict[str, float]:
    lt = lift_table(y, p, mrr)
    k = int(len(y) * 0.1)
    top = np.argsort(-p)[:k]
    return {
        "roc_auc": round(float(roc_auc_score(y, p)), 4),
        "pr_auc": round(float(average_precision_score(y, p)), 4),
        "brier": round(float(brier_score_loss(y, p)), 4),
        "base_rate": round(float(y.mean()), 4),
        "precision_at_top_decile": round(float(y[top].mean()), 4),
        "lift_at_top_decile": round(float(lt.loc[0, "lift"]), 2),
        "recall_at_top_decile": round(float(lt.loc[0, "cum_churner_capture"]), 4),
        "churned_mrr_captured_top_2_deciles": round(float(lt.loc[1, "cum_churned_mrr_capture"]), 4),
    }


@dataclass
class TrainResult:
    model: Pipeline
    model_name: str
    metrics: dict
    lift: pd.DataFrame
    importance: pd.DataFrame
    validation: pd.DataFrame        # y, p, mrr for charts


def time_split(df: pd.DataFrame, validation_months: int, horizon: int):
    months = np.sort(df["snapshot_month"].unique())
    val_start = months[-validation_months]
    purge_before = pd.Timestamp(val_start) - pd.DateOffset(months=horizon)
    train = df[df["snapshot_month"] < purge_before]
    val = df[df["snapshot_month"] >= val_start]
    return train, val, pd.Timestamp(val_start), purge_before


def train(feature_df: pd.DataFrame, cfg: dict) -> TrainResult:
    rs = cfg["random_state"]
    labelled = feature_df[feature_df["is_label_observed"].astype(bool)].copy()
    labelled = labelled[labelled["tenure_months"] >= cfg["min_tenure_months"]]
    labelled[TARGET] = labelled[TARGET].astype(int)

    tr, va, val_start, purge_before = time_split(labelled, cfg["validation_months"], cfg["horizon_months"])
    log.info("train rows %s (snapshots < %s), validation rows %s (snapshots >= %s)",
             f"{len(tr):,}", purge_before.date(), f"{len(va):,}", val_start.date())

    X_tr, y_tr = prepare(tr), tr[TARGET].to_numpy()
    X_va, y_va = prepare(va), va[TARGET].to_numpy()
    mrr_va = va["mrr_usd"].to_numpy()

    candidates = {
        "logistic_regression": make_baseline(rs),
        "gradient_boosting": make_gbm(rs),
    }
    scores: dict[str, dict] = {}
    for name, pipe in candidates.items():
        pipe.fit(X_tr, y_tr)
        scores[name] = evaluate(y_va, pipe.predict_proba(X_va)[:, 1], mrr_va)
        log.info("%-20s %s", name, scores[name])

    best_name = max(scores, key=lambda n: scores[n]["pr_auc"])
    log.info("selected %s", best_name)

    # Calibrate the winner (isotonic, cross-validated on the training window)
    calibrated = CalibratedClassifierCV(candidates[best_name], method="isotonic", cv=3)
    calibrated.fit(X_tr, y_tr)
    p_va = calibrated.predict_proba(X_va)[:, 1]
    final_metrics = evaluate(y_va, p_va, mrr_va)

    imp = permutation_importance(calibrated, X_va, y_va, scoring="average_precision",
                                 n_repeats=5, random_state=rs, n_jobs=-1)
    importance = (pd.DataFrame({"feature": FEATURES, "importance": imp.importances_mean,
                                "std": imp.importances_std})
                  .assign(label=lambda d: d["feature"].map(FEATURE_LABELS))
                  .sort_values("importance", ascending=False))

    # Refit on all labelled history for production scoring
    production = CalibratedClassifierCV(_fresh(best_name, rs), method="isotonic", cv=3)
    production.fit(prepare(labelled), labelled[TARGET].to_numpy())

    metrics = {
        "model": best_name,
        "horizon_months": cfg["horizon_months"],
        "train_rows": int(len(tr)),
        "validation_rows": int(len(va)),
        "train_snapshot_range": [str(tr["snapshot_month"].min().date()), str(tr["snapshot_month"].max().date())],
        "validation_snapshot_range": [str(va["snapshot_month"].min().date()),
                                      str(va["snapshot_month"].max().date())],
        "candidates": scores,
        "validation": final_metrics,
    }
    validation = pd.DataFrame({"y": y_va, "p": p_va, "mrr": mrr_va})
    return TrainResult(production, best_name, metrics, lift_table(y_va, p_va, mrr_va), importance, validation)


def _fresh(name: str, rs: int) -> Pipeline:
    return make_gbm(rs) if name == "gradient_boosting" else make_baseline(rs)


# --------------------------------------------------------------------------- #
# Charts for the README / model report
# --------------------------------------------------------------------------- #
INK, MUTED, GRID, BASE = "#0b0b0b", "#898781", "#e1e0d9", "#c3c2b7"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"


def _style(ax, title: str) -> None:
    ax.set_title(title, loc="left", fontsize=12, color=INK, pad=12, fontweight="bold")
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(BASE)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.xaxis.label.set_color(MUTED)
    ax.yaxis.label.set_color(MUTED)


def save_charts(result: TrainResult, out_dir: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "DejaVu Sans", "figure.facecolor": "#fcfcfb",
                         "axes.facecolor": "#fcfcfb", "savefig.facecolor": "#fcfcfb"})

    # 1. Feature importance
    top = result.importance.head(12).iloc[::-1]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.barh(top["label"], top["importance"], color=BLUE, height=0.6)
    _style(ax, "What drives churn risk (permutation importance, PR-AUC drop)")
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.grid(axis="y", visible=False)
    ax.tick_params(axis="y", colors=INK)
    fig.tight_layout()
    fig.savefig(out_dir / "feature_importance.png", dpi=150)
    plt.close(fig)

    # 2. Cumulative capture (gains) chart
    lt = result.lift
    x = np.concatenate([[0], lt["decile"].to_numpy() * 10])
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(x, np.concatenate([[0], lt["cum_churner_capture"]]) * 100, color=BLUE, lw=2,
            marker="o", ms=5, label="Churned accounts captured")
    ax.plot(x, np.concatenate([[0], lt["cum_churned_mrr_capture"]]) * 100, color=ORANGE, lw=2,
            marker="o", ms=5, label="Churned MRR captured")
    ax.plot([0, 100], [0, 100], color=MUTED, lw=1, ls="--", label="Random targeting")
    _style(ax, "Gains: share of churn caught by contacting the riskiest X% of accounts")
    ax.set_xlabel("% of accounts contacted (ranked by churn score)")
    ax.set_ylabel("% captured")
    ax.legend(frameon=False, fontsize=9, labelcolor=INK)
    fig.tight_layout()
    fig.savefig(out_dir / "gains_chart.png", dpi=150)
    plt.close(fig)

    # 3. Calibration
    v = result.validation
    frac, mean_pred = calibration_curve(v["y"], v["p"], n_bins=10, strategy="quantile")
    fig, ax = plt.subplots(figsize=(6, 5))
    ax.plot([0, max(mean_pred.max(), frac.max())], [0, max(mean_pred.max(), frac.max())],
            color=MUTED, lw=1, ls="--", label="Perfect calibration")
    ax.plot(mean_pred, frac, color=AQUA, lw=2, marker="o", ms=6, label="Model")
    _style(ax, "Calibration on out-of-time validation")
    ax.set_xlabel("Predicted churn probability")
    ax.set_ylabel("Observed churn rate")
    ax.legend(frameon=False, fontsize=9, labelcolor=INK)
    fig.tight_layout()
    fig.savefig(out_dir / "calibration.png", dpi=150)
    plt.close(fig)


def save_artifacts(result: TrainResult, artifacts: Path) -> None:
    model_dir = artifacts / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(result.model, model_dir / "churn_model.joblib")
    (model_dir / "metrics.json").write_text(json.dumps(result.metrics, indent=2))
    result.lift.to_csv(model_dir / "lift_table.csv", index=False)
    result.importance.to_csv(model_dir / "feature_importance.csv", index=False)
    log.info("saved model + metrics to %s", model_dir)
