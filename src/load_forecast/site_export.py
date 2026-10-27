"""Export production forecast results, metrics, and monitoring data for the static public site.

Reads served forecasts, choices, scores, CUSUM drift statistics, and RTE D-1 from
the database (or pre-production replay data), and writes a compact JSON payload
for the static dashboard on GitHub Pages.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import URL, Engine, create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from load_forecast.cutoff import delivery_slots
from load_forecast.delivery_quality import degraded_input_reasons
from load_forecast.monitoring import (
    MIN_SLOTS,
    H,
    K,
    cusum,
    standardize,
)
from load_forecast.production_scoring import read_actuals

LOGGER = logging.getLogger(__name__)
PARIS = ZoneInfo("Europe/Paris")
FINAL_VINTAGES = ("consolidated", "definitive")


def get_reader_engine() -> Engine:
    """Create an engine from explicit environment credentials only."""
    db_url = os.environ.get("LFF_DATABASE_URL")
    if db_url:
        return create_engine(db_url)
    for setting in ("POSTGRES_USER", "POSTGRES_PASSWORD"):
        if not os.environ.get(setting):
            raise ValueError(f"Missing {setting}; load the environment with scripts/load_env.ps1")
    return create_engine(URL.create(
        "postgresql+psycopg", username=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        database=os.environ.get("POSTGRES_DB", "load_forecast"),
    ))


def _table_name(engine: Engine, schema: str, name: str) -> str:
    """Map forecast fixtures to unqualified SQLite tables."""
    if engine.dialect.name == "sqlite" and schema not in inspect(engine).get_schema_names():
        return f'"{name}"'
    return f'"{schema}"."{name}"'


def _read_table(engine: Engine, query, params: dict | None = None) -> pd.DataFrame:
    """Treat an absent relation as empty, while propagating all other failures."""
    try:
        with engine.connect() as connection:
            result = connection.execute(query, params or {})
            return pd.DataFrame(result.fetchall(), columns=result.keys())
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) == "42P01":
            return pd.DataFrame()
        if isinstance(error.orig, sqlite3.OperationalError) and str(error.orig).startswith("no such table:"):
            return pd.DataFrame()
        raise


def _round_mw(value: Any) -> int | None:
    """Round a power value to integer MW, returning None if missing or non-finite."""
    if value is None:
        return None
    try:
        val = float(value)
        if math.isnan(val) or not math.isfinite(val):
            return None
        return int(round(val))
    except (TypeError, ValueError):
        return None


def _load_choices_from_db(engine: Engine, max_days: int = 120) -> pd.DataFrame:
    """Load latest serving choices per delivery day from forecast.choices."""
    t_choices = _table_name(engine, "forecast", "choices")
    query = text(f"""
        SELECT delivery_day, choice_hash, kind, run_id, fallback_id, chosen_at
        FROM {t_choices}
        ORDER BY delivery_day DESC, chosen_at DESC
    """)
    df = _read_table(engine, query)
    if df.empty:
        return pd.DataFrame()
    df["delivery_day"] = pd.to_datetime(df["delivery_day"]).dt.date
    df = df.drop_duplicates(subset=["delivery_day"], keep="first")
    return df.head(max_days).sort_values("delivery_day").reset_index(drop=True)


def _load_served_values_from_db(engine: Engine, choices_df: pd.DataFrame) -> pd.DataFrame:
    """Load half-hour forecast values for the selected choices."""
    if choices_df.empty:
        return pd.DataFrame()

    primary_run_ids = choices_df.loc[
        (choices_df["kind"] == "primary") & choices_df["run_id"].notna(), "run_id"
    ].tolist()
    fallback_ids = choices_df.loc[
        (choices_df["kind"] == "fallback") & choices_df["fallback_id"].notna(), "fallback_id"
    ].tolist()

    records = []

    if primary_run_ids:
        t_values = _table_name(engine, "forecast", "values")
        placeholders = ", ".join(f":r_{i}" for i in range(len(primary_run_ids)))
        params = {f"r_{i}": r_id for i, r_id in enumerate(primary_run_ids)}
        query = text(f"""
            SELECT v.run_id, v.value_time, v.local_slot,
                   v.point AS forecast_mw, v.lower AS lower_mw, v.upper AS upper_mw
            FROM {t_values} v
            WHERE v.run_id IN ({placeholders})
            ORDER BY v.value_time ASC
        """)
        prim_df = _read_table(engine, query, params)
        if prim_df.empty:
            return pd.DataFrame()

        run_to_day = dict(zip(choices_df["run_id"], choices_df["delivery_day"], strict=False))
        prim_df["delivery_day"] = prim_df["run_id"].map(run_to_day)
        prim_df["kind"] = "primary"
        records.append(prim_df)

    if fallback_ids:
        t_fallbacks = _table_name(engine, "forecast", "fallbacks")
        placeholders = ", ".join(f":fb_{i}" for i in range(len(fallback_ids)))
        params = {f"fb_{i}": fb_id for i, fb_id in enumerate(fallback_ids)}
        query = text(f"""
            SELECT fallback_id, payload
            FROM {t_fallbacks}
            WHERE fallback_id IN ({placeholders})
        """)
        fb_df = _read_table(engine, query, params)

        fb_rows = []
        for _, row in fb_df.iterrows():
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            deliv_day = payload.get("delivery_day")
            if isinstance(deliv_day, str):
                deliv_day = date.fromisoformat(deliv_day)
            for item in payload.get("values", []):
                fb_rows.append({
                    "run_id": row["fallback_id"],
                    "value_time": item.get("value_time"),
                    "local_slot": item.get("local_slot"),
                    "forecast_mw": item.get("point"),
                    "lower_mw": None,
                    "upper_mw": None,
                    "delivery_day": deliv_day,
                    "kind": "fallback",
                })
        if fb_rows:
            records.append(pd.DataFrame(fb_rows))

    if not records:
        return pd.DataFrame()

    combined = pd.concat(records, ignore_index=True)
    combined["value_time"] = pd.to_datetime(combined["value_time"], utc=True)
    return combined.sort_values("value_time").reset_index(drop=True)


def _load_actual_and_rte(engine: Engine, min_time: datetime, max_time: datetime) -> pd.DataFrame:
    """Use the scoring reader's latest raw versions and D12 source filtering."""
    # An absent raw relation is an empty installation, not a connection failure.
    inspector = inspect(engine)
    if "raw" not in inspector.get_schema_names() or not inspector.has_table("eco2mix", schema="raw"):
        return pd.DataFrame()
    with engine.connect() as connection:
        frames = read_actuals(connection, min_time.astimezone(PARIS).date(),
                              max_time.astimezone(PARIS).date(), datetime.now(UTC))
    rows = pd.concat([frames["realtime"], frames["consolidated"]]).reset_index()
    rows = rows.loc[rows.value_time.between(min_time, max_time)].copy()
    # Prefer final consumption when present; keep forecast-only slots for tomorrow.
    rows["has_actual"] = rows.consommation.notna()
    return (rows.sort_values("has_actual", kind="stable")
            .drop_duplicates("value_time", keep="last").sort_values("value_time"))


def _load_shadow_values(engine: Engine) -> pd.DataFrame:
    """Read only the selected shadow state run, never an arbitrary retry."""
    states = _table_name(engine, "forecast", "state_runs")
    values = _table_name(engine, "forecast", "values")
    rows = _read_table(engine, text(f"""
        SELECT s.delivery_day, v.value_time, v.point AS shadow_mw
        FROM {states} s JOIN {values} v ON v.run_id = s.run_id
        WHERE s.variant = 'A' AND v.variant = 'A'
        ORDER BY v.value_time
    """))
    if not rows.empty:
        rows["delivery_day"] = pd.to_datetime(rows.delivery_day).dt.date
        rows["value_time"] = pd.to_datetime(rows.value_time, utc=True)
    return rows


def _load_stored_scores(engine: Engine) -> pd.DataFrame:
    """Load latest stored scores per delivery day from forecast.scores."""
    t_scores = _table_name(engine, "forecast", "scores")
    query = text(f"""
        SELECT delivery_day, choice_hash, actual_vintage, scored_at, metrics
        FROM {t_scores}
        ORDER BY delivery_day DESC,
                 CASE actual_vintage WHEN 'definitive' THEN 0
                                     WHEN 'consolidated' THEN 1 ELSE 2 END,
                 scored_at DESC
    """)
    df = _read_table(engine, query)
    if df.empty:
        return pd.DataFrame()
    df["delivery_day"] = pd.to_datetime(df["delivery_day"]).dt.date
    return df.drop_duplicates(subset=["delivery_day"], keep="first")


def _load_delivery_quality(engine: Engine, choices: pd.DataFrame) -> dict:
    """Read stored flags and recover the same diagnosis for older immutable runs."""
    ids = choices.loc[choices["kind"].eq("primary"), "run_id"].dropna().tolist()
    if not ids:
        return {}
    results = _table_name(engine, "forecast", "run_results")
    experts = _table_name(engine, "forecast", "experts")
    placeholders = ", ".join(f":run_{i}" for i in range(len(ids)))
    rows = _read_table(engine, text(f"""
        SELECT r.run_id, r.flags, r.last_consumption_slot,
               EXISTS (SELECT 1 FROM {experts} e
                       WHERE e.run_id = r.run_id AND e.backup = 'backup_chain') AS backup_used
        FROM {results} r
        WHERE r.run_id IN ({placeholders})
    """), {f"run_{i}": run_id for i, run_id in enumerate(ids)})
    by_id = {r.run_id: r for r in rows.itertuples()}
    quality = {}
    for choice in choices.itertuples():
        if choice.kind != "primary" or choice.run_id not in by_id:
            continue
        row = by_id[choice.run_id]
        flags = json.loads(row.flags) if isinstance(row.flags, str) else row.flags
        quality[choice.delivery_day] = (flags.get("degraded_inputs") or degraded_input_reasons(
            choice.delivery_day, row.last_consumption_slot, bool(row.backup_used)))
    return quality


def _load_replay_scores(replay_source: Path | str | None) -> pd.DataFrame:
    """Load pre-production replay scores if available."""
    if replay_source is None:
        env_replay = os.environ.get("LFF_REPLAY_SCORES_PARQUET")
        if env_replay and os.path.exists(env_replay):
            replay_source = Path(env_replay)
        else:
            env_dir = os.environ.get("LFF_REPLAY_DIR")
            if env_dir and os.path.exists(Path(env_dir) / "replay_scores.parquet"):
                replay_source = Path(env_dir) / "replay_scores.parquet"

    if replay_source is None or isinstance(replay_source, pd.DataFrame):
        return pd.DataFrame()

    p = Path(replay_source)
    if p.is_dir():
        p = p / "replay_scores.parquet"
    elif not p.exists() or p.name != "replay_scores.parquet":
        alt = p.parent / "replay_scores.parquet"
        if alt.exists():
            p = alt

    if not p.exists() or not p.is_file():
        return pd.DataFrame()

    df = pd.read_parquet(p)
    if "delivery_day" in df.columns:
        df["delivery_day"] = pd.to_datetime(df["delivery_day"]).dt.date
    return df


def _load_replay_data(replay_source: Path | str | pd.DataFrame | None,
                      variant: str = "B") -> pd.DataFrame:
    """Load pre-production replay data from a parquet file or DataFrame."""
    if replay_source is None:
        env_replay = os.environ.get("LFF_REPLAY_PARQUET")
        if env_replay and os.path.exists(env_replay):
            replay_source = Path(env_replay)

    if replay_source is None:
        return pd.DataFrame()

    if isinstance(replay_source, (str, Path)):
        p = Path(replay_source)
        if variant == "A" and p.name == "forecasts_B.parquet":
            p = p.with_name("forecasts_A.parquet")
        if p.is_dir():
            for candidate in ["replay_forecasts.parquet", f"forecasts_{variant}.parquet"]:
                if (p / candidate).exists():
                    p = p / candidate
                    break
        if not p.exists() or not p.is_file():
            LOGGER.warning("Replay parquet path not found: %s", p)
            return pd.DataFrame()
        df = pd.read_parquet(p)
    elif isinstance(replay_source, pd.DataFrame):
        df = replay_source.copy()
    else:
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    if "variant" in df.columns:
        df = df[df["variant"] == variant]
    elif variant == "A":
        return pd.DataFrame()

    col_map = {
        "point": "forecast_mw",
        "lower": "lower_mw",
        "upper": "upper_mw",
        "consommation": "actual_mw",
        "actual": "actual_mw",
        "prevision_j1": "rte_mw",
        "rte_d1": "rte_mw",
    }
    for old_col, new_col in col_map.items():
        if old_col in df.columns and new_col not in df.columns:
            df[new_col] = df[old_col]

    if "value_time" in df.columns:
        df["value_time"] = pd.to_datetime(df["value_time"], utc=True)
        if "delivery_day" not in df.columns:
            df["delivery_day"] = df["value_time"].dt.tz_convert(PARIS).dt.date
    if "delivery_day" in df.columns:
        df["delivery_day"] = pd.to_datetime(df["delivery_day"]).dt.date

    df["kind"] = "replay"
    df["is_replay"] = True
    return df


def _live_comparisons(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize complete paired live days over calendar windows, before rounding."""
    live = [r for r in records if not r["is_replay"]]
    complete = [r for r in live
                if r["kind"] != "none"
                and r["n_slots"] == len(delivery_slots(r["delivery_day"], "30min"))
                and r["actual_vintage"] in (*FINAL_VINTAGES, "realtime", "real-time")
                and all(r[k] is not None and math.isfinite(r[k])
                        for k in ("model_mae", "rte_mae"))]
    if complete:
        last_scored = max(r["delivery_day"] for r in complete)
        live = [r for r in live if r["delivery_day"] <= last_scored]
    result = {}
    for mode in ("all_live", "excluding_degraded"):
        paired = [r for r in complete if mode == "all_live" or not r["degraded_inputs"]]
        history = []
        summary = {}
        for day in live:
            end = day["delivery_day"]
            window = [r for r in paired if end - timedelta(days=27) <= r["delivery_day"] <= end]
            n = len(window)
            provisional = sum(r["comparison_variant"] == "A" for r in window)
            model = float(np.mean([r["model_mae"] for r in window])) if n else None
            rte = float(np.mean([r["rte_mae"] for r in window])) if n else None
            common = {"status": "ok" if n else "awaiting_complete",
                      "total_days": n, "provisional_days": provisional,
                      "is_provisional": provisional > 0}
            better = sum(r["model_mae"] < r["rte_mae"] for r in window)
            summary = {
                "rolling_28d_mae": common | {
                    "model_mw": _round_mw(model), "rte_mw": _round_mw(rte),
                    "gap_mw": _round_mw(model - rte) if n else None,
                    "relative_gain_pct": round(100 * (rte - model) / rte, 1) if n and rte else None,
                },
                "beating_rte": common | {"pct": round(100 * better / n, 1) if n else None,
                                         "better_days": better if n else None},
            }
            history.append({"date": day["date"], "model_rolling_mae": _round_mw(model),
                            "rte_rolling_mae": _round_mw(rte), "comparison_days": n,
                            "provisional_days": provisional, "is_replay": False,
                            "actual_vintage": day["actual_vintage"]})
        result[mode] = {"header_tiles": summary, "history": history}
    return result


def build_site_payload(
    engine: Engine | None = None,
    replay_source: Path | str | pd.DataFrame | None = None,
    days_back: int = 90,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Build the full static JSON document from database and replay data."""
    now_paris = datetime.now(PARIS)

    # 1. Load choices and values from DB if available
    db_choices = pd.DataFrame()
    db_served_values = pd.DataFrame()
    actual_and_rte = pd.DataFrame()
    stored_scores = pd.DataFrame()
    shadow_values = pd.DataFrame()
    delivery_quality = {}

    if engine is not None:
        db_choices = _load_choices_from_db(engine, max_days=days_back + 14)
        if not db_choices.empty:
            db_served_values = _load_served_values_from_db(engine, db_choices)
            stored_scores = _load_stored_scores(engine)
            shadow_values = _load_shadow_values(engine)
            delivery_quality = _load_delivery_quality(engine, db_choices)

    # 2. Load replay data
    replay_df = _load_replay_data(replay_source)
    replay_a = _load_replay_data(replay_source, variant="A")
    shadow_map = {}
    if not replay_a.empty:
        live_days = set(db_choices.delivery_day) if not db_choices.empty else set()
        shadow_map.update(replay_a.loc[~replay_a.delivery_day.isin(live_days)]
                          .set_index("value_time").forecast_mw.to_dict())
    if not shadow_values.empty:
        shadow_map.update(shadow_values.set_index("value_time").shadow_mw.to_dict())

    # Check for empty state
    if db_choices.empty and replay_df.empty:
        return {
            "status": "empty",
            "updated_at": now_paris.isoformat(),
            "header_tiles": {
                "tomorrow_peak": None,
                "rolling_28d_mae": None,
                "beating_rte": None,
                "delivery_counts": {
                    "served": 0,
                    "fallback": 0,
                    "none": 0,
                    "replay": 0,
                    "total": 0,
                },
            },
            "tomorrow": None,
            "recent_7d": [],
            "recent_14d": [],
            "daily_mae_28d": [],
            "rolling_mae_history": [],
            "vintage_switch_date": None,
            "cusum_history": {
                "h": float(H),
                "k": float(K),
                "vintage": "consolidated",
                "series": [],
            },
        }

    # 3. Combine choices / delivery days
    served_days = set(db_choices["delivery_day"]) if not db_choices.empty else set()
    replay_days = set(replay_df["delivery_day"]) if not replay_df.empty else set()
    replay_only_days = sorted(replay_days - served_days)

    all_delivery_days = sorted(served_days | replay_days)
    if as_of is not None:
        all_delivery_days = [d for d in all_delivery_days if d <= as_of]

    if not all_delivery_days:
        return build_site_payload(engine=None, replay_source=None)

    # Slice to last 90 days + tomorrow
    target_days = all_delivery_days[-(days_back + 1):]

    # 4. Fetch actual and RTE from DB for needed range
    if engine is not None and target_days:
        first_day = min(target_days) - timedelta(days=1)
        last_day = max(target_days) + timedelta(days=2)
        min_utc = datetime(first_day.year, first_day.month, first_day.day, tzinfo=UTC)
        max_utc = datetime(last_day.year, last_day.month, last_day.day, 23, 59, tzinfo=UTC)
        actual_and_rte = _load_actual_and_rte(engine, min_utc, max_utc)

    actual_map: dict[pd.Timestamp, float] = {}
    rte_map: dict[pd.Timestamp, float] = {}
    vintage_map: dict[pd.Timestamp, str] = {}
    if not actual_and_rte.empty:
        actual_and_rte = actual_and_rte.set_index("value_time")
        if "consommation" in actual_and_rte.columns:
            actual_map = actual_and_rte["consommation"].dropna().to_dict()
        if "prevision_j1" in actual_and_rte.columns:
            rte_map = actual_and_rte["prevision_j1"].dropna().to_dict()
        vintage_map = actual_and_rte["source_dataset"].map({
            "eco2mix-national-cons-def": "consolidated",
            "eco2mix-national-tr": "realtime",
        }).fillna("unknown").to_dict()

    # 5. Extract tomorrow (the latest delivery day)
    tomorrow_day = target_days[-1]
    tomorrow_slots: list[dict[str, Any]] = []
    tomorrow_is_replay = tomorrow_day in replay_only_days

    if tomorrow_day in served_days and not db_served_values.empty:
        t_sub = db_served_values[db_served_values["delivery_day"] == tomorrow_day]
        for _, row in t_sub.sort_values("value_time").iterrows():
            vt = row["value_time"]
            local_dt = vt.astimezone(PARIS)
            time_str = local_dt.strftime("%H:%M")
            rte_val = rte_map.get(vt)
            tomorrow_slots.append({
                "time": time_str,
                "value_time": vt.isoformat(),
                "forecast_mw": _round_mw(row.get("forecast_mw")),
                "lower_mw": _round_mw(row.get("lower_mw")),
                "upper_mw": _round_mw(row.get("upper_mw")),
                "rte_mw": _round_mw(rte_val),
            })
    elif tomorrow_day in replay_only_days and not replay_df.empty:
        t_sub = replay_df[replay_df["delivery_day"] == tomorrow_day]
        for _, row in t_sub.sort_values("value_time").iterrows():
            vt = row["value_time"]
            local_dt = vt.astimezone(PARIS)
            time_str = local_dt.strftime("%H:%M")
            rte_val = row.get("rte_mw")
            if rte_val is None or (isinstance(rte_val, (float, np.floating)) and np.isnan(rte_val)):
                rte_val = rte_map.get(vt)
            tomorrow_slots.append({
                "time": time_str,
                "value_time": vt.isoformat(),
                "forecast_mw": _round_mw(row.get("forecast_mw")),
                "lower_mw": _round_mw(row.get("lower_mw")),
                "upper_mw": _round_mw(row.get("upper_mw")),
                "rte_mw": _round_mw(rte_val),
            })

    # Tomorrow peak
    peak_mw = None
    peak_time = None
    valid_points = [
        (s["forecast_mw"], s["time"]) for s in tomorrow_slots if s["forecast_mw"] is not None
    ]
    if valid_points:
        best = max(valid_points, key=lambda x: x[0])
        peak_mw = best[0]
        peak_time = best[1]

    # 6. Build daily metrics across target_days
    daily_records: list[dict[str, Any]] = []
    all_slot_records: list[dict[str, Any]] = []

    # Map stored scores if available
    scores_by_day = {}
    replay_scores_df = _load_replay_scores(replay_source)
    if not replay_scores_df.empty:
        for _, row in replay_scores_df.iterrows():
            deliv_day = row["delivery_day"]
            variant = row.get("variant", "B")
            metrics = scores_by_day.setdefault(deliv_day, {})
            metrics.update({
                "model_a" if variant == "A" else "served": {
                    "mae": row.get("model_mae"),
                    "mean_error": row.get("model_mean_error"),
                },
                "rte_d1": {
                    "mae": row.get("rte_d1_mae"),
                    "mean_error": row.get("rte_d1_mean_error"),
                },
                "n_common_slots": row.get("n_common_slots", 48),
                "actual_vintage": row.get("actual_vintage", "unknown"),
            })

    if not stored_scores.empty:
        for _, row in stored_scores.iterrows():
            m = row["metrics"]
            if isinstance(m, str):
                m = json.loads(m)
            if not isinstance(m, dict):
                raise ValueError("Stored score metrics must be a JSON object")
            m["actual_vintage"] = row.get("actual_vintage", "unknown")
            scores_by_day[row["delivery_day"]] = m

    for deliv_day in target_days:
        is_rep = deliv_day in replay_only_days
        kind = "replay" if is_rep else "none"
        if not is_rep and not db_choices.empty:
            choice_match = db_choices[db_choices["delivery_day"] == deliv_day]
            if not choice_match.empty:
                kind = choice_match.iloc[0]["kind"]

        day_slots_df = pd.DataFrame()
        if not is_rep and not db_served_values.empty:
            day_slots_df = db_served_values[db_served_values["delivery_day"] == deliv_day]
        elif is_rep and not replay_df.empty:
            day_slots_df = replay_df[replay_df["delivery_day"] == deliv_day]
        if day_slots_df.empty and not shadow_values.empty:
            day_slots_df = shadow_values.loc[shadow_values.delivery_day.eq(deliv_day)].copy()
            day_slots_df["forecast_mw"] = None

        model_mae = None
        rte_mae = None
        mean_err = None
        rte_mean_err = None
        n_slots_scored = 0
        actual_vintage = "unknown"

        slot_rows = []
        for _, row in day_slots_df.iterrows():
            vt = row["value_time"]
            # Fresh raw rows take precedence over an archived replay actual.
            actual = actual_map.get(vt, row.get("actual_mw"))
            rte = rte_map.get(vt, row.get("rte_mw"))
            vintage = vintage_map.get(vt, row.get("actual_vintage", "unknown"))
            slot_rows.append({"value_time": vt, "actual_mw": actual, "rte_mw": rte,
                              "served_forecast_mw": row.get("forecast_mw"),
                              "actual_vintage": vintage})
        known_vintages = {r["actual_vintage"] for r in slot_rows
                          if _round_mw(r["actual_mw"]) is not None}
        if known_vintages & set(FINAL_VINTAGES):
            actual_vintage = next(v for v in FINAL_VINTAGES if v in known_vintages)
        elif known_vintages & {"realtime", "real-time"}:
            actual_vintage = "realtime"

        # Check if pre-stored score exists on the selected scale.
        stored_m = scores_by_day.get(deliv_day)
        if actual_vintage == "unknown" and stored_m:
            actual_vintage = stored_m.get("actual_vintage", "unknown")
        comparison_variant = "A" if actual_vintage in ("realtime", "real-time") else "B"
        metric_key = "model_a" if comparison_variant == "A" else "served"
        same_vintage = stored_m and (
            stored_m.get("actual_vintage") == actual_vintage
            or {stored_m.get("actual_vintage"), actual_vintage} <= {"realtime", "real-time"})
        if same_vintage and stored_m.get(metric_key):
            model_mae = stored_m[metric_key].get("mae")
            mean_err = stored_m[metric_key].get("mean_error")
            n_slots_scored = stored_m.get("n_common_slots", 0)
            actual_vintage = stored_m.get("actual_vintage", "unknown")
            if "rte_d1" in stored_m and stored_m["rte_d1"]:
                rte_mae = stored_m["rte_d1"].get("mae")
                rte_mean_err = stored_m["rte_d1"].get("mean_error")

        # Otherwise calculate from slots
        for row in slot_rows:
            row["forecast_mw"] = (shadow_map.get(row["value_time"])
                                  if comparison_variant == "A" else row["served_forecast_mw"])
            row["comparison_variant"] = comparison_variant
            if (row["actual_vintage"] != actual_vintage
                    and {row["actual_vintage"], actual_vintage} != {"realtime", "real-time"}):
                row["actual_mw"] = None
        if (model_mae is None or np.isnan(model_mae)) and slot_rows:
            pts = []
            acts = []
            rtes = []
            vintages = set()
            for r in slot_rows:
                vt = r["value_time"]
                f_val = r.get("forecast_mw")
                a_val = r.get("actual_mw")
                r_val = r.get("rte_mw")

                if all(value is not None and math.isfinite(value)
                       for value in (f_val, a_val, r_val)):
                    pts.append(float(f_val))
                    acts.append(float(a_val))
                    vintages.add(r.get("actual_vintage", vintage_map.get(vt, "unknown")))
                    if r_val is not None and not math.isnan(r_val):
                        rtes.append(float(r_val))

            if len(pts) >= MIN_SLOTS:
                n_slots_scored = len(pts)
                p_arr = np.array(pts)
                a_arr = np.array(acts)
                err_arr = p_arr - a_arr
                model_mae = float(np.mean(np.abs(err_arr)))
                mean_err = float(np.mean(err_arr))
                if len(vintages) == 1:
                    actual_vintage = vintages.pop()
                elif vintages & {"realtime", "real-time"}:
                    actual_vintage = "realtime"
                if len(rtes) == len(pts):
                    r_arr = np.array(rtes)
                    rte_mae = float(np.mean(np.abs(r_arr - a_arr)))
                    rte_mean_err = float(np.mean(r_arr - a_arr))

        # Also collect slot points for recent 14 days
        for r in slot_rows:
            vt = r["value_time"]
            f_val = r.get("forecast_mw")
            a_val = r.get("actual_mw")
            r_val = r.get("rte_mw")
            local_str = vt.astimezone(PARIS).strftime("%Y-%m-%d %H:%M")
            all_slot_records.append({
                "time": local_str,
                "date": deliv_day.isoformat(),
                "actual_mw": _round_mw(a_val),
                "forecast_mw": _round_mw(f_val),
                "served_forecast_mw": _round_mw(r["served_forecast_mw"]),
                "comparison_variant": comparison_variant,
                "rte_mw": _round_mw(r_val),
                "is_replay": is_rep,
                "actual_vintage": r.get("actual_vintage", vintage_map.get(vt, actual_vintage)),
                "degraded_inputs": delivery_quality.get(deliv_day, []),
            })

        daily_records.append({
            "date": deliv_day.isoformat(),
            "delivery_day": deliv_day,
            "kind": kind,
            "is_replay": is_rep,
            "model_mae": model_mae,
            "rte_mae": rte_mae,
            "mean_error": mean_err,
            "rte_mean_error": rte_mean_err,
            "actual_vintage": actual_vintage,
            "comparison_variant": comparison_variant,
            "n_slots": n_slots_scored,
            "has_actuals": bool(known_vintages),
            "degraded_inputs": delivery_quality.get(deliv_day, []),
        })

    # 7. Compute rolling 28-day MAE and beating share
    scored_daily_records = [
        r for r in daily_records if r["model_mae"] is not None and not math.isnan(r["model_mae"])
    ]
    comparison_records = [
        r for r in scored_daily_records
        if r["actual_vintage"] in (*FINAL_VINTAGES, "realtime", "real-time")
        and r["rte_mae"] is not None and math.isfinite(r["rte_mae"])
    ]

    # Map daily MAEs for rolling window
    for r in daily_records:
        cur_day = r["delivery_day"]
        win_start = cur_day - timedelta(days=27)
        win_records = [
            x for x in comparison_records
            if win_start <= x["delivery_day"] <= cur_day and x["model_mae"] is not None
        ]
        if win_records:
            r["model_rolling_mae"] = float(np.mean([x["model_mae"] for x in win_records]))
            rte_vals = [x["rte_mae"] for x in win_records if x["rte_mae"] is not None]
            r["rte_rolling_mae"] = float(np.mean(rte_vals)) if rte_vals else None
        else:
            r["model_rolling_mae"] = None
            r["rte_rolling_mae"] = None
        r["comparison_days"] = len(win_records)
        r["provisional_days"] = sum(x["comparison_variant"] == "A" for x in win_records)

    # Header tiles: trailing 28-day summary
    summary = {"status": "awaiting_consolidated", "total_days": 0,
               "provisional_days": 0, "is_provisional": False}
    rolling_28d_tile = summary | {
        "model_mw": None, "rte_mw": None, "gap_mw": None, "relative_gain_pct": None,
    }
    beating_rte_tile = summary | {"pct": None, "better_days": None}
    latest_scored_day = target_days[-1]
    observed_records = [r for r in daily_records if r["has_actuals"]
                        or (r["model_mae"] is not None and math.isfinite(r["model_mae"]))]
    if observed_records:
        latest_scored_day = observed_records[-1]["delivery_day"]
        win_28_start = latest_scored_day - timedelta(days=27)
        recent_28_days = [
            x for x in observed_records
            if win_28_start <= x["delivery_day"] <= latest_scored_day
        ]
        if recent_28_days:
            paired_days = [x for x in comparison_records
                           if win_28_start <= x["delivery_day"] <= latest_scored_day]
            provisional_days = sum(x["comparison_variant"] == "A"
                                   for x in (paired_days or recent_28_days))
            summary = {"status": "ok" if paired_days else "awaiting_consolidated",
                       "total_days": len(paired_days), "provisional_days": provisional_days,
                       "is_provisional": provisional_days > 0}
            rolling_28d_tile = summary | {
                "model_mw": None, "rte_mw": None, "gap_mw": None, "relative_gain_pct": None,
            }
            beating_rte_tile = summary | {"pct": None, "better_days": None}
            m_maes = [x["model_mae"] for x in paired_days]
            r_maes = [x["rte_mae"] for x in paired_days]
            if m_maes and r_maes:
                mean_m = float(np.mean(m_maes))
                mean_r = float(np.mean(r_maes))
                gap = mean_m - mean_r
                gain_pct = round(((mean_r - mean_m) / mean_r) * 100.0, 1) if mean_r else 0.0
                rolling_28d_tile |= {
                    "model_mw": _round_mw(mean_m),
                    "rte_mw": _round_mw(mean_r),
                    "gap_mw": _round_mw(gap),
                    "relative_gain_pct": gain_pct,
                }

                # Share of days beating RTE
                paired = [
                    (x["model_mae"], x["rte_mae"]) for x in paired_days
                    if x["model_mae"] is not None and x["rte_mae"] is not None
                ]
                if paired:
                    better_count = sum(1 for m, r in paired if m < r)
                    beating_rte_tile |= {
                        "pct": round((better_count / len(paired)) * 100.0, 1),
                        "better_days": better_count,
                        "total_days": len(paired),
                    }

    # Delivery counts
    counts = {"served": 0, "fallback": 0, "none": 0, "replay": 0, "total": 0}
    for r in daily_records:
        k = r["kind"]
        if k in ("primary", "served"):
            counts["served"] += 1
        elif k == "fallback":
            counts["fallback"] += 1
        elif k == "replay":
            counts["replay"] += 1
        else:
            counts["none"] += 1
        counts["total"] += 1

    # 8. Compute CUSUM series (consolidated vintage only)
    cusum_series: list[dict[str, Any]] = []
    consolidated_daily_records = [
        r for r in scored_daily_records
        if r.get("actual_vintage", "consolidated") == "consolidated"
    ]
    if consolidated_daily_records:
        min_date = min(r["delivery_day"] for r in consolidated_daily_records)
        max_date = max(r["delivery_day"] for r in consolidated_daily_records)
        full_cal = pd.date_range(min_date, max_date, freq="D").date

        # e_d = actual - forecast = -mean_error
        err_dict = {
            r["delivery_day"]: (-r["mean_error"] if r["mean_error"] is not None else np.nan)
            for r in consolidated_daily_records
        }
        rep_dict = {r["delivery_day"]: r["is_replay"] for r in consolidated_daily_records}

        err_series = pd.Series([err_dict.get(d, np.nan) for d in full_cal], index=full_cal)
        z = standardize(err_series)
        cusum_df = cusum(z, h=H, k=K)

        for d in full_cal:
            row = cusum_df.loc[d]
            cusum_series.append({
                "date": d.isoformat(),
                "s_plus": round(float(row["s_plus"]), 2),
                "s_minus": round(float(row["s_minus"]), 2),
                "alarm": bool(row["alarm"]),
                "event": str(row["event"]),
                "is_replay": bool(rep_dict.get(d, False)),
            })

    # 9. Recent slot series (7 days and 14 days up to day before tomorrow)
    recent_7d_slots: list[dict[str, Any]] = []
    recent_14d_slots: list[dict[str, Any]] = []
    if all_slot_records:
        scored_dates = sorted({
            r["date"] for r in all_slot_records
            if r["actual_mw"] is not None
        })
        if not scored_dates:
            scored_dates = sorted({r["date"] for r in all_slot_records if r["actual_mw"] is not None})
        last_7_dates = set(scored_dates[-7:]) if scored_dates else set()
        recent_7d_slots = [
            r for r in all_slot_records if r["date"] in last_7_dates
        ]
        last_14_dates = set(scored_dates[-14:]) if scored_dates else set()
        recent_14d_slots = [
            r for r in all_slot_records if r["date"] in last_14_dates
        ]

    # Format rolling MAE history for json
    rolling_mae_history = [
        {
            "date": r["date"],
            "model_mae": _round_mw(r["model_mae"]),
            "rte_mae": _round_mw(r["rte_mae"]),
            "model_mean_error": _round_mw(r["mean_error"]),
            "rte_mean_error": _round_mw(r["rte_mean_error"]),
            "model_rolling_mae": _round_mw(r["model_rolling_mae"]),
            "rte_rolling_mae": _round_mw(r["rte_rolling_mae"]),
            "comparison_days": r["comparison_days"],
            "degraded_inputs": r["degraded_inputs"],
            "provisional_days": r["provisional_days"],
            "comparison_variant": r["comparison_variant"],
            "is_replay": r["is_replay"],
            "actual_vintage": r.get("actual_vintage", "consolidated"),
        }
        for r in daily_records
        if r["delivery_day"] != tomorrow_day or r["model_mae"] is not None
    ]

    # Format daily MAE for last 28 days for grouped bar chart
    scored_last_28 = [
        r for r in scored_daily_records
        if r["delivery_day"] >= latest_scored_day - timedelta(days=27)
    ][-28:]
    daily_mae_28d = [
        {
            "date": r["date"],
            "model_mae": _round_mw(r["model_mae"]),
            "rte_mae": _round_mw(r["rte_mae"]),
            "model_mean_error": _round_mw(r["mean_error"]),
            "rte_mean_error": _round_mw(r["rte_mean_error"]),
            "is_replay": r["is_replay"],
            "degraded_inputs": r["degraded_inputs"],
            "actual_vintage": r.get("actual_vintage", "consolidated"),
            "comparison_variant": r["comparison_variant"],
        }
        for r in scored_last_28
    ]

    # Find switch date where actuals switch from consolidated to real-time
    vintage_switch_date = None
    for i in range(1, len(daily_records)):
        prev_v = daily_records[i - 1].get("actual_vintage")
        cur_v = daily_records[i].get("actual_vintage")
        if prev_v == "consolidated" and cur_v in ("realtime", "real-time"):
            vintage_switch_date = daily_records[i]["date"]
            break

    return {
        "status": "ok",
        "updated_at": now_paris.isoformat(),
        "header_tiles": {
            "tomorrow_peak": {
                "delivery_day": tomorrow_day.isoformat(),
                "peak_mw": peak_mw,
                "peak_time": peak_time,
                "is_replay": tomorrow_is_replay,
            } if peak_mw is not None else None,
            "rolling_28d_mae": rolling_28d_tile,
            "beating_rte": beating_rte_tile,
            "delivery_counts": counts,
        },
        "tomorrow": {
            "delivery_day": tomorrow_day.isoformat(),
            "is_replay": tomorrow_is_replay,
            "degraded_inputs": delivery_quality.get(tomorrow_day, []),
            "slots": tomorrow_slots,
        } if tomorrow_slots else None,
        "recent_7d": recent_7d_slots,
        "recent_14d": recent_14d_slots,
        "daily_mae_28d": daily_mae_28d,
        "degraded_deliveries": [
            {"date": str(day), "reasons": delivery_quality[day]}
            for day in target_days if delivery_quality.get(day)
        ],
        "rolling_mae_history": rolling_mae_history,
        "live_comparisons": _live_comparisons(daily_records),
        "vintage_switch_date": vintage_switch_date,
        "cusum_history": {
            "h": float(H),
            "k": float(K),
            "vintage": "consolidated",
            "series": cusum_series,
        },
    }


def _json_safe(value: Any) -> Any:
    """Replace NaN and infinite floats by null: browsers reject NaN in JSON."""
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def export_site_data(
    engine: Engine | None = None,
    output_path: Path | str = Path("site/data/latest.json"),
    replay_source: Path | str | pd.DataFrame | None = None,
    days_back: int = 90,
    as_of: date | None = None,
) -> Path:
    """Export public site payload to a small JSON file."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if engine is None and "LFF_DATABASE_URL" in os.environ:
        engine = get_reader_engine()

    payload = build_site_payload(
        engine=engine,
        replay_source=replay_source,
        days_back=days_back,
        as_of=as_of,
    )

    with out.open("w", encoding="utf-8") as f:
        json.dump(_json_safe(payload), f, separators=(",", ":"), ensure_ascii=False,
                  allow_nan=False)

    LOGGER.info("Exported site data to %s (%d bytes)", out, out.stat().st_size)
    return out


def main() -> int:
    """CLI entrypoint for exporting site data."""
    parser = argparse.ArgumentParser(description="Export site forecast and metric data")
    parser.add_argument("--output", type=Path, default=Path("site/data/latest.json"),
                        help="Path to output json file")
    parser.add_argument("--replay", type=Path, default=None,
                        help="Path to replay parquet file")
    parser.add_argument("--days", type=int, default=90,
                        help="Number of days of history to include")
    args = parser.parse_args()

    engine = get_reader_engine()

    export_site_data(
        engine=engine,
        output_path=args.output,
        replay_source=args.replay,
        days_back=args.days,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
