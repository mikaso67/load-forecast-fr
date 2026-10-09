"""Verification and analysis script for RTE D-1 bias investigation.

Connects to PostgreSQL as lff_agent, measures RTE D-1 and intraday D bias across multiple
dimensions up to 2024-12-31, checks for overlaps between real-time and consolidated datasets,
verifies real-time bias since 2026-07-01, evaluates leak-free bias corrections on 2023 and 2024,
and computes bootstrap confidence intervals.
"""

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from load_forecast import evaluation as ev
from load_forecast.database import clean_table as get_clean_table
from load_forecast.labels import read_labels
from load_forecast.rte_reference import prior_year_hourly_reference

PARIS_TZ = ZoneInfo("Europe/Paris")


def get_connection() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "load_forecast"),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        options="-c timezone=UTC",
        connect_timeout=5,
        autocommit=True,
    )



def query_bias_by_year(conn: psycopg.Connection, table: str) -> pd.DataFrame:
    query = f"""
    SELECT
        EXTRACT(YEAR FROM value_time AT TIME ZONE 'Europe/Paris')::int AS paris_year,
        COUNT(*) AS total_slots,
        COUNT(consommation) AS n_cons,
        COUNT(prevision_j1) AS n_prev_j1,
        COUNT(prevision_j) AS n_prev_j,
        ROUND(AVG(prevision_j1 - consommation)::numeric, 2) AS mean_bias_j1_mw,
        ROUND(STDDEV(prevision_j1 - consommation)::numeric, 2) AS std_bias_j1_mw,
        ROUND(AVG(ABS(prevision_j1 - consommation))::numeric, 2) AS mae_j1_mw,
        ROUND(SQRT(AVG(POWER(prevision_j1 - consommation, 2)))::numeric, 2) AS rmse_j1_mw,
        ROUND((100.0 * AVG(ABS(prevision_j1 - consommation)::numeric / NULLIF(consommation, 0)))::numeric, 3) AS mape_j1_pct,
        ROUND((100.0 * AVG((prevision_j1 - consommation)::numeric / NULLIF(consommation, 0)))::numeric, 3) AS mean_bias_j1_pct,
        ROUND(AVG(prevision_j - consommation)::numeric, 2) AS mean_bias_j_mw,
        ROUND(AVG(ABS(prevision_j - consommation))::numeric, 2) AS mae_j_mw,
        ROUND(AVG(prevision_j1)::numeric, 2) AS mean_prev_j1_mw,
        ROUND(AVG(consommation)::numeric, 2) AS mean_cons_mw
    FROM {table}
    WHERE (value_time AT TIME ZONE 'Europe/Paris') < '2025-01-01 00:00:00'
    GROUP BY paris_year
    ORDER BY paris_year;
    """
    with conn.cursor() as cur:
        cur.execute(query)
        cols = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def query_bias_2024_by_month(conn: psycopg.Connection, table: str) -> pd.DataFrame:
    query = f"""
    SELECT
        EXTRACT(MONTH FROM value_time AT TIME ZONE 'Europe/Paris')::int AS month,
        COUNT(*) AS n_slots,
        ROUND(AVG(prevision_j1 - consommation)::numeric, 2) AS mean_bias_mw,
        ROUND(STDDEV(prevision_j1 - consommation)::numeric, 2) AS std_bias_mw,
        ROUND(AVG(ABS(prevision_j1 - consommation))::numeric, 2) AS mae_mw,
        ROUND(SQRT(AVG(POWER(prevision_j1 - consommation, 2)))::numeric, 2) AS rmse_mw,
        ROUND((100.0 * AVG(ABS(prevision_j1 - consommation)::numeric / NULLIF(consommation, 0)))::numeric, 3) AS mape_pct,
        ROUND((100.0 * AVG((prevision_j1 - consommation)::numeric / NULLIF(consommation, 0)))::numeric, 3) AS mean_bias_pct,
        ROUND(AVG(prevision_j1)::numeric, 2) AS mean_prev_j1_mw,
        ROUND(AVG(consommation)::numeric, 2) AS mean_cons_mw
    FROM {table}
    WHERE (value_time AT TIME ZONE 'Europe/Paris') >= '2024-01-01 00:00:00'
      AND (value_time AT TIME ZONE 'Europe/Paris') < '2025-01-01 00:00:00'
    GROUP BY month
    ORDER BY month;
    """
    with conn.cursor() as cur:
        cur.execute(query)
        cols = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def query_bias_2024_by_hour(conn: psycopg.Connection, table: str) -> pd.DataFrame:
    query = f"""
    SELECT
        EXTRACT(HOUR FROM value_time AT TIME ZONE 'Europe/Paris')::int AS paris_hour,
        COUNT(*) AS n_slots,
        ROUND(AVG(prevision_j1 - consommation)::numeric, 2) AS mean_bias_mw,
        ROUND(STDDEV(prevision_j1 - consommation)::numeric, 2) AS std_bias_mw,
        ROUND(AVG(ABS(prevision_j1 - consommation))::numeric, 2) AS mae_mw,
        ROUND(SQRT(AVG(POWER(prevision_j1 - consommation, 2)))::numeric, 2) AS rmse_mw,
        ROUND((100.0 * AVG(ABS(prevision_j1 - consommation)::numeric / NULLIF(consommation, 0)))::numeric, 3) AS mape_pct,
        ROUND((100.0 * AVG((prevision_j1 - consommation)::numeric / NULLIF(consommation, 0)))::numeric, 3) AS mean_bias_pct,
        ROUND(AVG(prevision_j1)::numeric, 2) AS mean_prev_j1_mw,
        ROUND(AVG(consommation)::numeric, 2) AS mean_cons_mw
    FROM {table}
    WHERE (value_time AT TIME ZONE 'Europe/Paris') >= '2024-01-01 00:00:00'
      AND (value_time AT TIME ZONE 'Europe/Paris') < '2025-01-01 00:00:00'
    GROUP BY paris_hour
    ORDER BY paris_hour;
    """
    with conn.cursor() as cur:
        cur.execute(query)
        cols = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def query_bias_2024_by_day_type(conn: psycopg.Connection, table: str) -> tuple[pd.DataFrame, ev.Bootstrap, int]:
    query = f"""
    SELECT value_time, consommation, prevision_j1
    FROM {table}
    WHERE (value_time AT TIME ZONE 'Europe/Paris') >= '2024-01-01 00:00:00'
      AND (value_time AT TIME ZONE 'Europe/Paris') < '2025-01-01 00:00:00'
    ORDER BY value_time;
    """
    with conn.cursor() as cur:
        cur.execute(query)
        df = pd.DataFrame(cur.fetchall(), columns=["value_time", "consommation", "prevision_j1"])
    df["value_time"] = pd.to_datetime(df["value_time"], utc=True)
    df = df.set_index("value_time")
    df["local_date"] = [t.astimezone(PARIS_TZ).date() for t in df.index]
    df["error"] = df["prevision_j1"] - df["consommation"]

    # Read observed SYNOP labels for 2024
    labels_df = read_labels(conn, date(2024, 1, 1), date(2025, 1, 1))
    cold_map = labels_df["cold"].dropna().to_dict()
    n_missing_labels = int(labels_df["cold"].isna().sum())

    # Map day types using protocol precedence: DST > holiday > bridge > cold > ordinary
    dates = pd.Index(df["local_date"].unique())
    day_labels = ev.label_days(dates, cold=cold_map)
    df["day_type"] = df["local_date"].map(day_labels)

    results = []
    for dt in ["ordinary", "cold", "holiday", "bridge", "dst"]:
        group = df[df["day_type"] == dt]
        n_days = group["local_date"].nunique()
        n_slots = len(group)
        mean_bias = group["error"].mean()
        std_bias = group["error"].std()
        mae = group["error"].abs().mean()
        rmse = float(np.sqrt((group["error"] ** 2).mean()))
        mape = (group["error"].abs() / group["consommation"]).mean() * 100.0
        mean_bias_pct = (group["error"] / group["consommation"]).mean() * 100.0
        results.append({
            "day_type": dt,
            "n_days": n_days,
            "n_slots": n_slots,
            "mean_bias_mw": round(mean_bias, 2),
            "std_bias_mw": round(std_bias, 2),
            "mae_mw": round(mae, 2),
            "rmse_mw": round(rmse, 2),
            "mape_pct": round(mape, 3),
            "mean_bias_pct": round(mean_bias_pct, 3),
        })

    # cold_any row (all days flagged as cold, regardless of holiday/DST precedence)
    cold_dates = [d for d, is_c in cold_map.items() if is_c]
    group_cold_any = df[df["local_date"].isin(cold_dates)]
    results.append({
        "day_type": "cold_any",
        "n_days": group_cold_any["local_date"].nunique(),
        "n_slots": len(group_cold_any),
        "mean_bias_mw": round(group_cold_any["error"].mean(), 2),
        "std_bias_mw": round(group_cold_any["error"].std(), 2),
        "mae_mw": round(group_cold_any["error"].abs().mean(), 2),
        "rmse_mw": round(float(np.sqrt((group_cold_any["error"] ** 2).mean())), 2),
        "mape_pct": round((group_cold_any["error"].abs() / group_cold_any["consommation"]).mean() * 100.0, 3),
        "mean_bias_pct": round((group_cold_any["error"] / group_cold_any["consommation"]).mean() * 100.0, 3),
    })

    # 2024 full-year daily bias bootstrap
    daily_bias = df.groupby("local_date")["error"].mean()
    daily_bias.index = pd.to_datetime(daily_bias.index)
    daily_bias = daily_bias.asfreq("D")
    boot = ev.block_bootstrap(daily_bias)

    return pd.DataFrame(results), boot, n_missing_labels


def query_raw_overlap(conn: psycopg.Connection) -> dict:
    query_total = """
    SELECT
        COUNT(*) AS total_overlap_count
    FROM (
        SELECT value_time FROM raw.eco2mix WHERE source_dataset = 'eco2mix-national-cons-def'
        INTERSECT
        SELECT value_time FROM raw.eco2mix WHERE source_dataset = 'eco2mix-national-tr'
    ) s;
    """
    query_up_to_2024 = """
    SELECT
        COUNT(*) AS overlap_2024_count
    FROM (
        SELECT value_time FROM raw.eco2mix
        WHERE source_dataset = 'eco2mix-national-cons-def'
          AND value_time < '2025-01-01 00:00:00+00'
        INTERSECT
        SELECT value_time FROM raw.eco2mix
        WHERE source_dataset = 'eco2mix-national-tr'
          AND value_time < '2025-01-01 00:00:00+00'
    ) s;
    """
    query_bounds = """
    SELECT
        source_dataset,
        MIN(value_time) AS min_time,
        MAX(value_time) AS max_time,
        COUNT(*) AS total_rows
    FROM raw.eco2mix
    GROUP BY source_dataset
    ORDER BY source_dataset;
    """
    with conn.cursor() as cur:
        cur.execute(query_total)
        total_overlap = cur.fetchone()[0]
        cur.execute(query_up_to_2024)
        overlap_2024 = cur.fetchone()[0]
        cur.execute(query_bounds)
        bounds = cur.fetchall()

    return {
        "total_overlap": total_overlap,
        "overlap_2024": overlap_2024,
        "bounds": bounds,
    }


def query_realtime_bias_2026(conn: psycopg.Connection) -> pd.DataFrame:
    query = """
    SELECT
        TO_CHAR(date_trunc('month', value_time AT TIME ZONE 'Europe/Paris'), 'YYYY-MM') AS month_label,
        COUNT(*) AS n_slots,
        ROUND(AVG(prevision_j1 - consommation)::numeric, 2) AS mean_bias_mw,
        ROUND(STDDEV(prevision_j1 - consommation)::numeric, 2) AS std_bias_mw,
        ROUND(AVG(ABS(prevision_j1 - consommation))::numeric, 2) AS mae_mw,
        ROUND(SQRT(AVG(POWER(prevision_j1 - consommation, 2)))::numeric, 2) AS rmse_mw
    FROM raw.eco2mix
    WHERE source_dataset = 'eco2mix-national-tr'
      AND value_time >= '2026-07-01 00:00+02'
      AND prevision_j1 IS NOT NULL
      AND consommation IS NOT NULL
    GROUP BY 1
    ORDER BY 1;
    """
    with conn.cursor() as cur:
        cur.execute(query)
        cols = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def evaluate_series(actual: pd.Series, forecast: pd.Series) -> dict:
    err = forecast - actual
    mae = float(err.abs().mean())
    rmse = float(np.sqrt((err ** 2).mean()))
    mape = float((err.abs() / actual).mean() * 100.0)
    mean_err = float(err.mean())
    return {"mae": mae, "rmse": rmse, "mape": mape, "mean_error": mean_err}


def simulate_leak_free_corrections(conn: psycopg.Connection, table: str) -> dict:
    query = f"""
    SELECT value_time, consommation, prevision_j1
    FROM {table}
    WHERE (value_time AT TIME ZONE 'Europe/Paris') >= '2021-03-24 00:00:00'
      AND (value_time AT TIME ZONE 'Europe/Paris') < '2025-01-01 00:00:00'
    ORDER BY value_time;
    """
    with conn.cursor() as cur:
        cur.execute(query)
        df = pd.DataFrame(cur.fetchall(), columns=["value_time", "consommation", "prevision_j1"])

    df["value_time"] = pd.to_datetime(df["value_time"], utc=True)
    df = df.set_index("value_time")
    df["paris_time"] = df.index.tz_convert(PARIS_TZ)
    df["paris_date"] = df["paris_time"].dt.date
    df["paris_year"] = df["paris_time"].dt.year
    df["paris_hour"] = df["paris_time"].dt.hour
    df["error"] = df["prevision_j1"] - df["consommation"]

    df_2022 = df[df["paris_year"] == 2022]
    df_2023 = df[df["paris_year"] == 2023]
    df_2024 = df[df["paris_year"] == 2024]

    results = {
        "raw_rte": {
            2023: evaluate_series(df_2023["consommation"], df_2023["prevision_j1"]),
            2024: evaluate_series(df_2024["consommation"], df_2024["prevision_j1"]),
        }
    }

    # Candidate (a1): Prior calendar year flat mean bias
    bias_2022_flat = df_2022["error"].mean()
    bias_2023_flat = df_2023["error"].mean()
    corr_a1_2023 = df_2023["prevision_j1"] - bias_2022_flat
    corr_a1_2024 = df_2024["prevision_j1"] - bias_2023_flat
    results["candidate_a1_prior_year_flat"] = {
        2023: evaluate_series(df_2023["consommation"], corr_a1_2023),
        2024: evaluate_series(df_2024["consommation"], corr_a1_2024),
    }

    # Candidate (a2): Prior calendar year by hour-of-day
    corr_a2_2023 = prior_year_hourly_reference(
        df["consommation"], df["prevision_j1"], df_2023["prevision_j1"])
    corr_a2_2024 = prior_year_hourly_reference(
        df["consommation"], df["prevision_j1"], df_2024["prevision_j1"])
    results["candidate_a2_prior_year_hod"] = {
        2023: evaluate_series(df_2023["consommation"], corr_a2_2023),
        2024: evaluate_series(df_2024["consommation"], corr_a2_2024),
    }

    # Candidate (b): Rolling bias over errors older than consolidation delay L
    for L in [45, 60]:
        for W in [28, 56]:
            # flat
            series_23 = []
            for d in sorted(df_2023["paris_date"].unique()):
                cutoff_date = d - timedelta(days=L)
                slice_w = df[(df["paris_date"] <= cutoff_date) & (df["paris_date"] > cutoff_date - timedelta(days=W))]
                series_23.append(df_2023[df_2023["paris_date"] == d]["prevision_j1"] - slice_w["error"].mean())
            c_23 = pd.concat(series_23)

            series_24 = []
            for d in sorted(df_2024["paris_date"].unique()):
                cutoff_date = d - timedelta(days=L)
                slice_w = df[(df["paris_date"] <= cutoff_date) & (df["paris_date"] > cutoff_date - timedelta(days=W))]
                series_24.append(df_2024[df_2024["paris_date"] == d]["prevision_j1"] - slice_w["error"].mean())
            c_24 = pd.concat(series_24)

            results[f"candidate_b_L{L}d_W{W}d_flat"] = {
                2023: evaluate_series(df_2023["consommation"], c_23),
                2024: evaluate_series(df_2024["consommation"], c_24),
            }

            # by hour of day
            series_23_hod = []
            for d in sorted(df_2023["paris_date"].unique()):
                cutoff_date = d - timedelta(days=L)
                slice_w = df[(df["paris_date"] <= cutoff_date) & (df["paris_date"] > cutoff_date - timedelta(days=W))]
                b_hod = slice_w.groupby("paris_hour")["error"].mean()
                d_df = df_2023[df_2023["paris_date"] == d]
                series_23_hod.append(d_df["prevision_j1"] - d_df["paris_hour"].map(b_hod))
            c_23_hod = pd.concat(series_23_hod)

            series_24_hod = []
            for d in sorted(df_2024["paris_date"].unique()):
                cutoff_date = d - timedelta(days=L)
                slice_w = df[(df["paris_date"] <= cutoff_date) & (df["paris_date"] > cutoff_date - timedelta(days=W))]
                b_hod = slice_w.groupby("paris_hour")["error"].mean()
                d_df = df_2024[df_2024["paris_date"] == d]
                series_24_hod.append(d_df["prevision_j1"] - d_df["paris_hour"].map(b_hod))
            c_24_hod = pd.concat(series_24_hod)

            results[f"candidate_b_L{L}d_W{W}d_hod"] = {
                2023: evaluate_series(df_2023["consommation"], c_23_hod),
                2024: evaluate_series(df_2024["consommation"], c_24_hod),
            }

    # Oracle / Lookahead-leaking corrections (from previous round, L=0)
    for W in [7, 14, 28]:
        s24 = []
        for d in sorted(df_2024["paris_date"].unique()):
            cutoff_slot = ev.last_usable_slot_start(d)
            past_data = df.loc[df.index <= cutoff_slot]
            start_w = cutoff_slot - timedelta(days=W)
            w_slice = past_data.loc[past_data.index >= start_w]
            bias = (w_slice["prevision_j1"] - w_slice["consommation"]).mean()
            d_slots = df_2024[df_2024["paris_date"] == d]
            s24.append(d_slots["prevision_j1"] - bias)
        results[f"oracle_leaking_rolling_{W}d"] = {
            2024: evaluate_series(df_2024["consommation"], pd.concat(s24))
        }

    s24_hod = []
    for d in sorted(df_2024["paris_date"].unique()):
        cutoff_slot = ev.last_usable_slot_start(d)
        past_data = df.loc[df.index <= cutoff_slot]
        start_w = cutoff_slot - timedelta(days=28)
        w_slice = past_data.loc[past_data.index >= start_w].copy()
        w_slice["hour"] = w_slice.index.tz_convert(PARIS_TZ).hour
        b_hod = (w_slice["prevision_j1"] - w_slice["consommation"]).groupby(w_slice["hour"]).mean()
        d_slots = df_2024[df_2024["paris_date"] == d].copy()
        d_slots["hour"] = d_slots.index.tz_convert(PARIS_TZ).hour
        s24_hod.append(d_slots["prevision_j1"] - d_slots["hour"].map(b_hod))
    results["oracle_leaking_rolling_28d_hod"] = {
        2024: evaluate_series(df_2024["consommation"], pd.concat(s24_hod))
    }

    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--corrections-only', action='store_true',
                        help='Read only 2021-2024 and verify correction scores.')
    args = parser.parse_args()
    print("=== Verification and Benchmark Diagnostics ===")
    table = get_clean_table()
    print(f"Reading clean table: {table}")

    with get_connection() as conn:
        if args.corrections_only:
            corrections = simulate_leak_free_corrections(conn, table)
            scores = corrections['candidate_a2_prior_year_hod']
            print(f"RTE* a2 MAE: 2023={scores[2023]['mae']:.2f}, 2024={scores[2024]['mae']:.2f} MW")
            assert abs(scores[2024]['mae'] - 896.65) < 0.01
            assert abs(scores[2023]['mae'] - 989.43) < 0.01
            return
        print("\n1. Measuring multi-year bias (2021-2024)...")
        df_year = query_bias_by_year(conn, table)
        print(df_year.to_string(index=False))

        print("\n2. Measuring 2024 bias by month...")
        df_month = query_bias_2024_by_month(conn, table)
        print(df_month.to_string(index=False))

        print("\n3. Measuring 2024 bias by hour of day (Europe/Paris)...")
        df_hour = query_bias_2024_by_hour(conn, table)
        print(df_hour.to_string(index=False))

        print("\n4. Measuring 2024 bias by day type (with SYNOP cold labels)...")
        df_day_type, boot_2024, n_missing_labels = query_bias_2024_by_day_type(conn, table)
        print(df_day_type.to_string(index=False))
        print(f"2024 Daily Mean Bias Bootstrap: mean={boot_2024.mean:.2f} MW, 95% CI=[{boot_2024.low:.2f}, {boot_2024.high:.2f}] MW, {boot_2024.verdict}")
        print(f"Observed label-missing days in 2024: {n_missing_labels} (2024-02-05)")

        print("\n5. Checking dataset overlap in raw.eco2mix...")
        overlap_info = query_raw_overlap(conn)
        print(f"Total overlap across full raw table: {overlap_info['total_overlap']}")
        print(f"Overlap up to 2024-12-31: {overlap_info['overlap_2024']}")
        for b in overlap_info["bounds"]:
            print(f"Dataset: {b[0]}, min: {b[1]}, max: {b[2]}, rows: {b[3]}")

        print("\n6. Measuring RTE D-1 bias against real-time consumption (eco2mix-national-tr) since 2026-07-01...")
        df_tr = query_realtime_bias_2026(conn)
        print(df_tr.to_string(index=False))

        print("\n7. Evaluating leak-free bias corrections (2023 vs 2024)...")
        corrections = simulate_leak_free_corrections(conn, table)
        for k, v in corrections.items():
            if 2023 in v and 2024 in v:
                print(f"  {k:30s} | 2023: MAE={v[2023]['mae']:7.2f} MW, MeanErr={v[2023]['mean_error']:7.2f} MW | 2024: MAE={v[2024]['mae']:7.2f} MW, MeanErr={v[2024]['mean_error']:7.2f} MW")
            elif 2024 in v:
                print(f"  {k:30s} | 2023: N/A                           | 2024: MAE={v[2024]['mae']:7.2f} MW, MeanErr={v[2024]['mean_error']:7.2f} MW")

        # Assertions to ensure strict verification
        print("\n8. Executing automated verification assertions...")
        assert overlap_info["total_overlap"] == 0, "Expected zero overlap between tr and cons-def in raw.eco2mix"
        assert abs(float(df_year.loc[df_year["paris_year"] == 2024, "mae_j1_mw"].values[0]) - 1364.58) < 0.1, "2024 RTE MAE mismatch"
        assert abs(float(df_year.loc[df_year["paris_year"] == 2024, "mean_bias_j1_mw"].values[0]) - (-1146.25)) < 0.1, "2024 RTE bias mismatch"
        assert abs(corrections["candidate_a1_prior_year_flat"][2024]["mae"] - 971.99) < 0.1, "Candidate a1 2024 MAE mismatch"
        assert abs(corrections["candidate_a2_prior_year_hod"][2024]["mae"] - 896.65) < 0.1, "Candidate a2 2024 MAE mismatch"
        assert abs(corrections["candidate_a2_prior_year_hod"][2023]["mae"] - 989.43) < 0.1, "Candidate a2 2023 MAE mismatch"
        print("All automated verification assertions passed successfully!")


if __name__ == "__main__":
    main()
