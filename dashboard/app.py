"""Streamlit production monitoring dashboard.

Displays the last 90 days of served load forecasts against actual consumption and
RTE D-1, daily MAE headline and model scores, fallback and late deliveries, two-sided
CUSUM drift statistics, and production run history.
"""

import json
import os
from datetime import date
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

from load_forecast.monitoring import (
    H,
    K,
    cusum,
    daily_error,
    score_day,
    standardize,
)

ACCENT_BLUE = "#1f4e9c"
GREY_RTE = "#888888"
DARK_GREY = "#222222"
LIGHT_BLUE = "#5b84c4"
VARIANT_A_COLOR = "#d97706"


def get_engine():
    """Create a database engine from LFF_DATABASE_URL or environment variables."""
    db_url = os.environ.get("LFF_DATABASE_URL")
    if db_url:
        return create_engine(db_url)

    user = os.environ.get("POSTGRES_USER", "lff_agent")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("POSTGRES_PORT", "5432")
    dbname = os.environ.get("POSTGRES_DB", "load_forecast")
    return create_engine(f"postgresql+psycopg://{user}:{password}@{host}:{port}/{dbname}")


def table_name(engine, schema: str, name: str) -> str:
    """Return quoted schema.table or fallback to plain table for SQLite."""
    quoted_name = f'"{name}"' if name == "values" else name
    if engine.dialect.name == "sqlite":
        with engine.connect() as conn:
            try:
                conn.execute(text(f"SELECT 1 FROM {schema}.{quoted_name} LIMIT 1"))
                return f"{schema}.{quoted_name}"
            except Exception:
                return quoted_name
    return f"{schema}.{quoted_name}"


def _has_column(engine, tbl_str: str, col_name: str) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text(f"SELECT {col_name} FROM {tbl_str} LIMIT 0"))
            return True
    except Exception:
        return False


def load_runs_data(engine) -> pd.DataFrame:
    t_starts = table_name(engine, "forecast", "run_starts")
    t_results = table_name(engine, "forecast", "run_results")
    var_col = "s.variant" if _has_column(engine, t_starts, "variant") else "'B' AS variant"
    query = text(f"""
        SELECT s.delivery_day, {var_col}, s.run_id, s.kind, r.status, s.freeze_time, s.started_at,
               r.completed_at, r.weather_status, r.refit_day, r.mlflow_run_id, r.flags
        FROM {t_starts} s
        LEFT JOIN {t_results} r ON s.run_id = r.run_id
        ORDER BY s.delivery_day DESC, s.started_at DESC
        LIMIT 200
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn)
    return df


def load_choices_data(engine) -> pd.DataFrame:
    t_choices = table_name(engine, "forecast", "choices")
    query = text(f"""
        SELECT c.delivery_day, c.choice_hash, c.kind, c.run_id, c.fallback_id, c.chosen_at
        FROM {t_choices} c
        ORDER BY c.delivery_day DESC, c.chosen_at DESC
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn)
    if not df.empty:
        df = df.drop_duplicates(subset=["delivery_day"], keep="first")
    return df


def load_delivery_incidents(engine) -> pd.DataFrame:
    try:
        t_incidents = table_name(engine, "forecast", "delivery_incidents")
        query = text(f"""
            SELECT delivery_day, mirror_hash, file_hash, detected_at
            FROM {t_incidents}
            ORDER BY delivery_day DESC
        """)
        with engine.connect() as conn:
            return pd.read_sql(query, conn)
    except Exception:
        return pd.DataFrame()


def load_served_values(engine, choices_df: pd.DataFrame) -> pd.DataFrame:
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
        t_values = table_name(engine, "forecast", "values")
        placeholders = ", ".join(f":r_{i}" for i in range(len(primary_run_ids)))
        params = {f"r_{i}": r_id for i, r_id in enumerate(primary_run_ids)}
        query = text(f"""
            SELECT v.run_id, v.value_time, v.local_slot, v.point AS forecast_mw,
                   v.lower AS lower_mw, v.upper AS upper_mw
            FROM {t_values} v
            WHERE v.run_id IN ({placeholders})
            ORDER BY v.value_time ASC
        """)
        with engine.connect() as conn:
            prim_df = pd.read_sql(query, conn, params=params)

        choice_map = dict(zip(choices_df["run_id"], choices_df["delivery_day"], strict=False))
        prim_df["delivery_day"] = prim_df["run_id"].map(choice_map)
        prim_df["kind"] = "primary"
        records.append(prim_df)

    if fallback_ids:
        t_fallbacks = table_name(engine, "forecast", "fallbacks")
        placeholders = ", ".join(f":fb_{i}" for i in range(len(fallback_ids)))
        params = {f"fb_{i}": fb_id for i, fb_id in enumerate(fallback_ids)}
        query = text(f"""
            SELECT fallback_id, payload
            FROM {t_fallbacks}
            WHERE fallback_id IN ({placeholders})
        """)
        with engine.connect() as conn:
            fb_df = pd.read_sql(query, conn, params=params)

        fb_rows = []
        for _, row in fb_df.iterrows():
            payload = row["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    continue
            deliv_day = payload.get("delivery_day")
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
    return combined.sort_values("value_time")


def load_actual_and_rte(engine, min_time, max_time) -> pd.DataFrame:
    clean_table = os.environ.get("LFF_CLEAN_TABLE", "analytics.load_30min")
    candidates = [clean_table]
    if "." in clean_table:
        parts = clean_table.split(".")
        candidates.append(f'"{parts[0]}"."{parts[1]}"')
        candidates.append(parts[1])
        candidates.append(f'"{parts[1]}"')

    min_iso = min_time.isoformat() if hasattr(min_time, "isoformat") else str(min_time)
    max_iso = max_time.isoformat() if hasattr(max_time, "isoformat") else str(max_time)

    for candidate in candidates:
        query = text(f"""
            SELECT value_time, consommation, prevision_j1
            FROM {candidate}
            WHERE value_time >= :min_time AND value_time <= :max_time
            ORDER BY value_time ASC
        """)
        try:
            with engine.connect() as conn:
                df = pd.read_sql(query, conn, params={"min_time": min_iso, "max_time": max_iso})
            if not df.empty:
                df["value_time"] = pd.to_datetime(df["value_time"], utc=True)
                return df
        except Exception:
            continue
    return pd.DataFrame()


def load_model_values_by_variant(engine, days: list[Any]) -> dict[str, pd.DataFrame]:
    """Load primary model values for variant A and variant B from state_runs."""
    if not days:
        return {"A": pd.DataFrame(), "B": pd.DataFrame()}

    t_state_runs = table_name(engine, "forecast", "state_runs")
    t_values = table_name(engine, "forecast", "values")
    placeholders = ", ".join(f":d_{i}" for i in range(len(days)))
    params = {
        f"d_{i}": (d.isoformat() if hasattr(d, "isoformat") else str(d))
        for i, d in enumerate(days)
    }

    has_var = _has_column(engine, t_state_runs, "variant")
    var_col = "sr.variant" if has_var else "'B' AS variant"
    query = text(f"""
        SELECT sr.delivery_day, {var_col}, v.value_time, v.local_slot,
               v.point AS forecast_mw, v.lower AS lower_mw, v.upper AS upper_mw
        FROM {t_state_runs} sr
        JOIN {t_values} v ON sr.run_id = v.run_id
        WHERE sr.delivery_day IN ({placeholders})
        ORDER BY v.value_time ASC
    """)
    try:
        with engine.connect() as conn:
            df = pd.read_sql(query, conn, params=params)
        if df.empty:
            return {"A": pd.DataFrame(), "B": pd.DataFrame()}
        df["value_time"] = pd.to_datetime(df["value_time"], utc=True)
        for col in ["forecast_mw", "lower_mw", "upper_mw"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df_a = df[df["variant"] == "A"].copy().reset_index(drop=True)
        df_b = df[df["variant"] == "B"].copy().reset_index(drop=True)
        return {"A": df_a, "B": df_b}
    except Exception:
        return {"A": pd.DataFrame(), "B": pd.DataFrame()}


def compute_daily_scores(
    served_values: pd.DataFrame,
    actual_and_rte: pd.DataFrame,
    model_values_by_variant: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Compute daily MAE and errors using score_day from the monitoring module."""
    if served_values.empty or actual_and_rte.empty:
        return pd.DataFrame()

    actual_map = actual_and_rte.set_index("value_time")
    records = []

    for deliv_day, s_group in served_values.groupby("delivery_day"):
        deliv_date = date.fromisoformat(str(deliv_day)) if isinstance(deliv_day, str) else deliv_day
        s_slots = s_group.set_index("value_time").sort_index()
        common_times = s_slots.index

        actual_slice = actual_map.reindex(common_times)
        actual_series = actual_slice["consommation"]
        rte_series = actual_slice["prevision_j1"]

        if actual_series.dropna().count() < 40:
            continue

        served_df = pd.DataFrame({
            "point": s_slots["forecast_mw"],
            "lower": s_slots["lower_mw"],
            "upper": s_slots["upper_mw"],
        }, index=common_times)

        df_b = model_values_by_variant.get("B", pd.DataFrame())
        if not df_b.empty:
            b_sub = df_b[df_b["delivery_day"].astype(str) == str(deliv_day)].set_index("value_time").sort_index()
            if not b_sub.empty:
                b_df = pd.DataFrame({
                    "point": b_sub["forecast_mw"],
                    "lower": b_sub.get("lower_mw", np.nan),
                    "upper": b_sub.get("upper_mw", np.nan),
                }, index=b_sub.index).reindex(common_times)
            else:
                b_df = served_df
        else:
            b_df = served_df

        score_b = score_day(
            actual=actual_series,
            rte=rte_series,
            served=served_df,
            model=b_df,
            rte_star=None,
            vintage="realtime",
        )

        served_mae = score_b["served"]["mae"] if score_b.get("served") else np.nan
        model_b_mae = score_b["model"]["mae"] if score_b.get("model") else served_mae
        rte_mae = score_b["rte_d1"]["mae"] if score_b.get("rte_d1") else np.nan
        err_b = daily_error(score_b)

        df_a = model_values_by_variant.get("A", pd.DataFrame())
        model_a_mae = np.nan
        err_a = np.nan
        if not df_a.empty:
            a_sub = df_a[df_a["delivery_day"].astype(str) == str(deliv_day)].set_index("value_time").sort_index()
            if not a_sub.empty:
                a_df = pd.DataFrame({
                    "point": a_sub["forecast_mw"],
                    "lower": a_sub.get("lower_mw", np.nan),
                    "upper": a_sub.get("upper_mw", np.nan),
                }, index=a_sub.index).reindex(common_times)

                score_a = score_day(
                    actual=actual_series,
                    rte=rte_series,
                    served=a_df,
                    model=None,
                    rte_star=None,
                    vintage="realtime",
                )
                model_a_mae = score_a["served"]["mae"] if score_a.get("served") else np.nan
                err_a = daily_error(score_a)

        records.append({
            "delivery_day": deliv_date,
            "served_mae": served_mae,
            "model_b_mae": model_b_mae,
            "model_a_mae": model_a_mae,
            "rte_mae": rte_mae,
            "err_b": err_b,
            "err_a": err_a,
        })

    if not records:
        return pd.DataFrame()

    out_df = pd.DataFrame(records).sort_values("delivery_day").reset_index(drop=True)
    return out_df


def compute_dashboard_cusum(daily_errors: pd.Series, h: float = H, k: float = K) -> pd.DataFrame:
    """Compute CUSUM drift statistics using the calibrated monitoring module."""
    if daily_errors.empty or daily_errors.dropna().empty:
        return pd.DataFrame()

    days_idx = pd.to_datetime(daily_errors.index)
    min_date = days_idx.min()
    max_date = days_idx.max()
    full_calendar = pd.date_range(min_date, max_date, freq="D")

    reindexed = pd.Series(daily_errors.to_numpy(dtype=float), index=days_idx).reindex(full_calendar)
    z = standardize(reindexed)
    return cusum(z, h=h, k=k)


def main():
    st.set_page_config(page_title="Electricity load forecast production dashboard", layout="wide")

    st.markdown(
        f"""
        <style>
        .main-header {{
            font-size: 24px;
            font-weight: 600;
            color: {ACCENT_BLUE};
            margin-bottom: 8px;
        }}
        .section-header {{
            font-size: 18px;
            font-weight: 600;
            color: #333333;
            margin-top: 24px;
            margin-bottom: 12px;
            border-bottom: 2px solid {ACCENT_BLUE};
            padding-bottom: 4px;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        '<div class="main-header">French electricity load forecast production dashboard</div>',
        unsafe_allow_html=True,
    )

    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        st.error(
            "Database connection failed. Ensure PostgreSQL is running or configure LFF_DATABASE_URL."
        )
        return

    try:
        t_starts = table_name(engine, "forecast", "run_starts")
        t_choices = table_name(engine, "forecast", "choices")
        with engine.connect() as conn:
            run_count = conn.execute(text(f"SELECT count(*) FROM {t_starts}")).scalar_one()
            choice_count = conn.execute(text(f"SELECT count(*) FROM {t_choices}")).scalar_one()
    except Exception:
        st.info(
            "The forecast database is currently empty. No tables or runs have been recorded."
        )
        return

    if run_count == 0 and choice_count == 0:
        st.info(
            "The forecast database is currently empty. No runs or serving choices have been recorded."
        )
        return

    runs_df = load_runs_data(engine)
    choices_df = load_choices_data(engine)
    incidents_df = load_delivery_incidents(engine)

    # Filter last 90 delivery days
    if not choices_df.empty:
        choices_90 = choices_df.head(90)
    else:
        choices_90 = pd.DataFrame()

    served_values = load_served_values(engine, choices_90)
    days = choices_90["delivery_day"].tolist() if not choices_90.empty else []
    models_by_variant = load_model_values_by_variant(engine, days)

    if not served_values.empty:
        min_time = served_values["value_time"].min()
        max_time = served_values["value_time"].max()
        actual_df = load_actual_and_rte(engine, min_time, max_time)
        if not actual_df.empty:
            merged = pd.merge(served_values, actual_df, on="value_time", how="left")
        else:
            merged = served_values.copy()
            merged["consommation"] = np.nan
            merged["prevision_j1"] = np.nan

        model_b_vals = models_by_variant.get("B", pd.DataFrame())
        if not model_b_vals.empty:
            merged = pd.merge(
                merged,
                model_b_vals[["value_time", "forecast_mw"]].rename(columns={"forecast_mw": "model_b_mw"}),
                on="value_time",
                how="left",
            )
        else:
            merged["model_b_mw"] = merged["forecast_mw"]

        model_a_vals = models_by_variant.get("A", pd.DataFrame())
        if not model_a_vals.empty:
            merged = pd.merge(
                merged,
                model_a_vals[["value_time", "forecast_mw"]].rename(columns={"forecast_mw": "model_a_mw"}),
                on="value_time",
                how="left",
            )
        else:
            merged["model_a_mw"] = np.nan

        for col in ["forecast_mw", "lower_mw", "upper_mw", "consommation", "prevision_j1", "model_b_mw", "model_a_mw"]:
            if col in merged.columns:
                merged[col] = pd.to_numeric(merged[col], errors="coerce")
    else:
        merged = pd.DataFrame()
        actual_df = pd.DataFrame()

    # Section 1: Served forecast vs actual and RTE D-1 (last 90 days)
    st.markdown(
        '<div class="section-header">1. Served forecast vs actual and RTE D-1 (last 90 days)</div>',
        unsafe_allow_html=True,
    )

    if not merged.empty:
        fig, ax = plt.subplots(figsize=(14, 4.5), dpi=100)
        ax.plot(
            merged["value_time"], merged["forecast_mw"],
            label="Served forecast (Variant B)", color=ACCENT_BLUE, linewidth=1.4,
        )

        if merged["model_a_mw"].notna().any():
            ax.plot(
                merged["value_time"], merged["model_a_mw"],
                label="Variant A shadow forecast", color=VARIANT_A_COLOR, linewidth=1.1, linestyle="-.", alpha=0.85,
            )

        if merged["consommation"].notna().any():
            ax.plot(
                merged["value_time"], merged["consommation"],
                label="Actual consumption", color=DARK_GREY, linewidth=1.1, alpha=0.9,
            )

        if merged["prevision_j1"].notna().any():
            ax.plot(
                merged["value_time"], merged["prevision_j1"],
                label="RTE D-1", color=GREY_RTE, linewidth=1.0, linestyle="--", alpha=0.8,
            )

        if merged["lower_mw"].notna().any() and merged["upper_mw"].notna().any():
            valid_interval = (merged["lower_mw"].notna() & merged["upper_mw"].notna()).to_numpy()
            ax.fill_between(
                merged["value_time"], merged["lower_mw"], merged["upper_mw"],
                where=valid_interval,
                color=ACCENT_BLUE, alpha=0.15, label="80% prediction interval",
            )

        ax.set_ylabel("Load (MW)")
        ax.set_xlabel("UTC timestamp")
        ax.grid(True, linestyle=":", alpha=0.5)
        ax.legend(loc="upper right", framealpha=0.9)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        fig.autofmt_xdate()
        st.pyplot(fig)
        plt.close(fig)
    else:
        st.info("No served forecast values available to display for the selected period.")

    # Section 2: Daily MAE: headline and model scores
    st.markdown(
        '<div class="section-header">2. Daily MAE: headline and model scores</div>',
        unsafe_allow_html=True,
    )

    daily_scores_df = compute_daily_scores(served_values, actual_df, models_by_variant)

    if not daily_scores_df.empty:
        col1, col2, col3, col4, col5 = st.columns(5)
        avg_served = daily_scores_df["served_mae"].mean()
        avg_model_b = daily_scores_df["model_b_mae"].mean()
        avg_model_a = (
            daily_scores_df["model_a_mae"].dropna().mean()
            if daily_scores_df["model_a_mae"].notna().any()
            else np.nan
        )
        avg_rte = (
            daily_scores_df["rte_mae"].dropna().mean()
            if daily_scores_df["rte_mae"].notna().any()
            else np.nan
        )

        col1.metric("Headline served MAE (B)", f"{avg_served:.1f} MW")
        col2.metric("Variant B model MAE", f"{avg_model_b:.1f} MW")
        if not np.isnan(avg_model_a):
            col3.metric("Variant A shadow MAE", f"{avg_model_a:.1f} MW")
        else:
            col3.metric("Variant A shadow MAE", "Not available")

        if not np.isnan(avg_rte):
            col4.metric("RTE D-1 MAE", f"{avg_rte:.1f} MW")
            col5.metric("MAE difference vs RTE", f"{avg_served - avg_rte:+.1f} MW")
        else:
            col4.metric("RTE D-1 MAE", "Not available")
            col5.metric("MAE difference vs RTE", "Not available")

        fig_mae, ax_mae = plt.subplots(figsize=(14, 3.5), dpi=100)
        days_plot = pd.to_datetime(daily_scores_df["delivery_day"])
        ax_mae.plot(
            days_plot, daily_scores_df["served_mae"],
            label="Headline served MAE (Variant B)", color=ACCENT_BLUE, marker="o", markersize=3,
        )
        ax_mae.plot(
            days_plot, daily_scores_df["model_b_mae"],
            label="Variant B model MAE", color=LIGHT_BLUE, linestyle="--",
        )
        if not np.isnan(avg_model_a):
            ax_mae.plot(
                days_plot, daily_scores_df["model_a_mae"],
                label="Variant A shadow MAE", color=VARIANT_A_COLOR, linestyle="-.", marker="s", markersize=3,
            )
        if not np.isnan(avg_rte):
            ax_mae.plot(
                days_plot, daily_scores_df["rte_mae"],
                label="RTE D-1 MAE", color=GREY_RTE, linestyle=":",
            )
        ax_mae.set_ylabel("Daily MAE (MW)")
        ax_mae.grid(True, linestyle=":", alpha=0.5)
        ax_mae.legend(loc="upper right")
        ax_mae.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        fig_mae.autofmt_xdate()
        st.pyplot(fig_mae)
        plt.close(fig_mae)
    else:
        st.info("Actual consumption is not yet available for daily score computation.")

    # Section 3: Operational delivery status
    st.markdown(
        '<div class="section-header">3. Operational delivery status</div>',
        unsafe_allow_html=True,
    )

    total_choices = len(choices_90)
    fallback_count = (choices_90["kind"] == "fallback").sum() if not choices_90.empty else 0
    none_count = (choices_90["kind"] == "none").sum() if not choices_90.empty else 0

    late_count = 0
    if not runs_df.empty:
        runs_eval = runs_df.dropna(subset=["freeze_time", "completed_at"]).copy()
        runs_eval["freeze_time"] = pd.to_datetime(runs_eval["freeze_time"], utc=True)
        runs_eval["completed_at"] = pd.to_datetime(runs_eval["completed_at"], utc=True)
        deadline = runs_eval["freeze_time"] + pd.Timedelta(minutes=30)
        late_count = (runs_eval["completed_at"] > deadline).sum()

    col_op1, col_op2, col_op3, col_op4 = st.columns(4)
    col_op1.metric("Observed deliveries", total_choices)
    col_op2.metric("Fallback deliveries", fallback_count)
    col_op3.metric("Unmet deliveries", none_count)
    col_op4.metric("Late completions", late_count)

    exceptions = []
    if not choices_90.empty:
        non_prim = choices_90[choices_90["kind"] != "primary"]
        for _, row in non_prim.iterrows():
            exceptions.append({
                "Delivery day": str(row["delivery_day"]),
                "Kind": row["kind"],
                "Detail": f"Served {row['kind']} choice",
            })

    if not incidents_df.empty:
        for _, row in incidents_df.iterrows():
            exceptions.append({
                "Delivery day": str(row["delivery_day"]),
                "Kind": "incident",
                "Detail": f"Mirror conflict: {row['mirror_hash'][:8]} vs {row['file_hash'][:8]}",
            })

    if exceptions:
        st.dataframe(pd.DataFrame(exceptions), use_container_width=True)
    else:
        st.write("Zero fallback, late, or conflicted deliveries recorded in the observed period.")

    # Section 4: Drift monitoring: two-sided CUSUM
    st.markdown(
        '<div class="section-header">4. Drift monitoring: two-sided CUSUM</div>',
        unsafe_allow_html=True,
    )

    if not daily_scores_df.empty and daily_scores_df["err_b"].notna().any():
        daily_err_b = pd.Series(
            daily_scores_df["err_b"].to_numpy(),
            index=pd.to_datetime(daily_scores_df["delivery_day"]),
        )
        cusum_b = compute_dashboard_cusum(daily_err_b, h=H, k=K)

        has_a = "err_a" in daily_scores_df.columns and daily_scores_df["err_a"].notna().any()
        if has_a:
            daily_err_a = pd.Series(
                daily_scores_df["err_a"].to_numpy(),
                index=pd.to_datetime(daily_scores_df["delivery_day"]),
            )
            cusum_a = compute_dashboard_cusum(daily_err_a, h=H, k=K)
        else:
            cusum_a = pd.DataFrame()

        fig_c, ax_c = plt.subplots(figsize=(14, 3.5), dpi=100)
        c_days = pd.to_datetime(cusum_b.index)
        ax_c.plot(
            c_days, cusum_b["s_plus"],
            label="Variant B S+ (positive drift)", color=ACCENT_BLUE, linewidth=1.5,
        )
        ax_c.plot(
            c_days, cusum_b["s_minus"],
            label="Variant B S- (negative drift)", color=LIGHT_BLUE, linewidth=1.5, linestyle="--",
        )

        if not cusum_a.empty:
            ax_c.plot(
                c_days, cusum_a["s_plus"],
                label="Variant A S+ (positive drift)", color=VARIANT_A_COLOR, linewidth=1.4, linestyle="-.",
            )
            ax_c.plot(
                c_days, cusum_a["s_minus"],
                label="Variant A S- (negative drift)", color="#f59e0b", linewidth=1.4, linestyle=":",
            )

        ax_c.axhline(H, color="#b22222", linestyle=":", label=f"Alarm threshold h = {H:.2f}")
        ax_c.set_ylabel("CUSUM statistic")
        ax_c.grid(True, linestyle=":", alpha=0.5)
        ax_c.legend(loc="upper left")
        ax_c.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        fig_c.autofmt_xdate()
        st.pyplot(fig_c)
        plt.close(fig_c)

        max_b = max(cusum_b["s_plus"].max(), cusum_b["s_minus"].max())
        if max_b > H:
            st.warning(
                f"Variant B drift alert active: cumulative standardized error exceeds "
                f"threshold h = {H:.2f} (current maximum: {max_b:.2f})."
            )
        else:
            st.write(f"Variant B in control: maximum CUSUM statistic is {max_b:.2f} (threshold h = {H:.2f}).")

        if not cusum_a.empty:
            max_a = max(cusum_a["s_plus"].max(), cusum_a["s_minus"].max())
            if max_a > H:
                st.warning(
                    f"Variant A drift alert active: cumulative standardized error exceeds "
                    f"threshold h = {H:.2f} (current maximum: {max_a:.2f})."
                )
            else:
                st.write(f"Variant A in control: maximum CUSUM statistic is {max_a:.2f} (threshold h = {H:.2f}).")
    else:
        st.info("CUSUM monitoring requires daily actual consumption data.")

    # Section 5: Production run status history
    st.markdown(
        '<div class="section-header">5. Production run status history</div>',
        unsafe_allow_html=True,
    )

    if not runs_df.empty:
        display_runs = runs_df.copy()
        st.dataframe(display_runs, use_container_width=True)
    else:
        st.write("No run events stored.")


if __name__ == "__main__":
    main()
