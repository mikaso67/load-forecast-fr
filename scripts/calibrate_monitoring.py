"""Calibrate the drift CUSUM threshold h before go-live (docs/monitoring.md).

Reads only stored forecasts of candidate 1 (EXP-009 aggregate): 2024 from the EXP-012 archive and
2025 from the TEST-2025 archive. No forecast is computed and the database is not opened.
Writes data/monitoring/calibration.json and prints a summary.

    python scripts/calibrate_monitoring.py [--years 20000] [--check]

With --check the script exits 1 unless the frozen constants of load_forecast.monitoring equal the
values recomputed here.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from load_forecast import monitoring as mon
from load_forecast.evaluation import local_dates

ROOT = Path(__file__).resolve().parents[1]
ARCHIVES = {
    2024: (ROOT / "data/experiments/EXP-012/intervals.csv", "point"),
    2025: (ROOT / "data/experiments/TEST-2025/predictions.csv", "candidate1"),
}
RTE_2025 = ROOT / "data/experiments/TEST-2025/predictions.csv"
OUT = ROOT / "data/monitoring/calibration.json"
BLOCKS = (7, 14, 28)
TARGET = 0.2
SEED = 20240
MAX_LAG = 60
SHIFTS = (0.5, 1.0)


def read_slots(path: Path, column: str) -> pd.DataFrame:
    frame = pd.read_csv(path, usecols=["value_time_utc", column, "actual"])
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop("value_time_utc"), utc=True))
    return frame.rename(columns={column: "forecast"}).dropna()


def daily_errors(slots: pd.DataFrame, year: int) -> pd.DataFrame:
    """e_d (actual minus forecast) and slot count per Paris day of `year`."""
    days = local_dates(slots.index)
    frame = pd.DataFrame({"error": (slots["actual"] - slots["forecast"]).to_numpy()}, index=days)
    daily = frame.groupby(level=0)["error"].agg(["mean", "size"])
    daily.index = pd.DatetimeIndex(daily.index)
    daily = daily[daily.index.year == year]
    calendar = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="D")
    return daily.reindex(calendar).rename(columns={"mean": "e", "size": "n_slots"})


def reference() -> pd.DataFrame:
    frames = [daily_errors(read_slots(*ARCHIVES[year]), year) for year in ARCHIVES]
    daily = pd.concat(frames)
    daily.loc[daily["n_slots"] < mon.MIN_SLOTS, "e"] = np.nan
    daily["season"] = [mon.season_of(day.date()) for day in daily.index]
    return daily


def season_table(daily: pd.DataFrame) -> tuple[dict, dict, dict]:
    grouped = daily.groupby("season")["e"]
    mean = {s: round(float(v), 4) for s, v in grouped.mean().items()}
    std = {s: round(float(v), 4) for s, v in grouped.std(ddof=1).items()}
    count = {s: int(v) for s, v in grouped.count().items()}
    return mean, std, count


def quantile_interval(maxima: np.ndarray, level: float) -> tuple[float, float]:
    """95 percent order-statistic interval of the `level` quantile of the simulated maxima."""
    n = len(maxima)
    ordered = np.sort(maxima)
    half = 1.96 * np.sqrt(n * level * (1 - level))
    low = int(np.floor(n * level - half))
    high = int(np.ceil(n * level + half))
    return float(ordered[max(low, 0)]), float(ordered[min(high, n - 1)])


def delay_summary(alarms: np.ndarray) -> dict:
    detected = alarms[alarms > 0]
    return {
        "p_detected_within_365": float(np.mean(alarms > 0)),
        "median_days": float(np.median(detected)) if len(detected) else None,
        "p90_days": float(np.percentile(detected, 90)) if len(detected) else None,
    }


def calibrate(z: pd.Series, n_years: int) -> dict:
    rng = np.random.default_rng(SEED)
    results = {}
    for block in BLOCKS:
        sampler = mon.BlockSampler.build(z, block)
        paths = sampler.years(n_years, rng)
        maxima = mon.cusum_maxima(paths)
        h = mon.smallest_h(maxima, TARGET)
        results[block] = {"paths": paths, "maxima": maxima, "h": h}
    iid = rng.standard_normal((n_years, mon.YEAR_DAYS))
    results["iid_normal"] = {"maxima": mon.cusum_maxima(iid)}
    results["iid_normal"]["h"] = mon.smallest_h(results["iid_normal"]["maxima"], TARGET)
    return results


def report(daily: pd.DataFrame, z: pd.Series, results: dict, n_years: int) -> dict:
    h_final = max(results[block]["h"] for block in BLOCKS)
    acf = mon.autocorrelation(z, MAX_LAG)
    n = len(z)
    long_run = {bw: mon.bartlett_variance(z, bw) for bw in (30, 60)}
    variance = float(z.var(ddof=0))
    per_block = {}
    for block in BLOCKS:
        result = results[block]
        paths = result["paths"]
        retained_bartlett = mon.bartlett_variance(z, block)
        retained_bootstrap = float(mon.YEAR_DAYS * paths.mean(axis=1).var())
        per_block[str(block)] = {
            "h_block": result["h"],
            "p_alarm_at_h_block": float(np.mean(result["maxima"] > result["h"])),
            "p_alarm_at_h_final": float(np.mean(result["maxima"] > h_final)),
            "p_alarm_at_h_iid_normal": float(np.mean(result["maxima"] > results["iid_normal"]["h"])),
            "h_quantile_95ci": quantile_interval(result["maxima"], 1 - TARGET),
            "retained_variance_bartlett": retained_bartlett,
            "retained_variance_bootstrap": retained_bootstrap,
            "ratio_long_run_60_to_bartlett": long_run[60] / retained_bartlett,
            "ratio_long_run_60_to_bootstrap": long_run[60] / retained_bootstrap,
            "delay_at_h_final": {str(shift): delay_summary(mon.first_alarm(paths + shift, h_final))
                                 for shift in SHIFTS},
        }
    iid = results["iid_normal"]
    mean, std, count = season_table(daily)
    return {
        "reference": {"years": list(ARCHIVES), "n_days": int(daily["e"].notna().sum()),
                      "n_missing_days": int(daily["e"].isna().sum()),
                      "min_slots_per_day": int(daily["n_slots"].min())},
        "season_mean": mean, "season_std": std, "season_days": count,
        "k": mon.K, "target": TARGET, "n_years": n_years, "seed": SEED,
        "calendar_window_days": mon.CALENDAR_WINDOW,
        "h": h_final,
        "h_iid_normal": iid["h"],
        "p_alarm_iid_normal_at_h_final": float(np.mean(iid["maxima"] > h_final)),
        "blocks": per_block,
        "acf": {str(lag): float(acf[lag]) for lag in range(1, MAX_LAG + 1)},
        "acf_95_white_noise_band": 1.96 / np.sqrt(n),
        "acf_lags_3_to_60_outside_band": int(np.sum(np.abs(acf[3:]) > 1.96 / np.sqrt(n))),
        "acf_max_abs_lags_3_to_60": float(np.max(np.abs(acf[3:]))),
        "truncated_acf_sum": {str(lag): float(1 + 2 * acf[1:lag + 1].sum())
                              for lag in (2, 7, 14, 28, 60)},
        "variance_z": variance,
        "long_run_variance_bartlett": {str(bw): v for bw, v in long_run.items()},
        "yearly_mean_z": {str(year): float(z[z.index.year == year].mean()) for year in ARCHIVES},
    }


def skill_2025() -> dict:
    """Descriptive: how often the item 2 band would have alerted in 2025 on consolidated data."""
    frame = pd.read_csv(RTE_2025, usecols=["value_time_utc", "candidate1", "actual", "rte_d1"])
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop("value_time_utc"), utc=True))
    frame = frame.dropna()
    days = pd.DatetimeIndex(local_dates(frame.index))
    served = (frame["candidate1"] - frame["actual"]).abs().groupby(days).mean()
    rte = (frame["rte_d1"] - frame["actual"]).abs().groupby(days).mean()
    keep = served.index.year == 2025
    band = mon.relative_skill(served[keep], rte[keep])
    return {"days_with_alert": int(band["alert"].sum()),
            "max_mean_difference": float(band["mean_difference"].max())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, default=20_000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    daily = reference()
    mean, std, _ = season_table(daily)
    if daily["e"].isna().any():
        raise SystemExit("A reference day has fewer than the minimum scored slots")
    z = mon.standardize(daily["e"], mean, std)
    results = calibrate(z, args.years)
    summary = report(daily, z, results, args.years)
    summary["relative_skill_2025_consolidated"] = skill_2025()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    brief = {key: summary[key] for key in ("season_mean", "season_std", "season_days", "h",
                                           "h_iid_normal", "p_alarm_iid_normal_at_h_final",
                                           "long_run_variance_bartlett", "variance_z", "truncated_acf_sum",
                                           "acf_lags_3_to_60_outside_band",
                                           "acf_max_abs_lags_3_to_60",
                                           "yearly_mean_z", "relative_skill_2025_consolidated")}
    brief["blocks"] = summary["blocks"]
    brief["acf_1_to_10"] = [round(summary["acf"][str(lag)], 3) for lag in range(1, 11)]
    print(json.dumps(brief, indent=2))
    if args.check:
        frozen = (mon.SEASON_MEAN, mon.SEASON_STD, mon.H)
        if frozen != (mean, std, summary["h"]):
            print("Frozen monitoring constants differ from this calibration", file=sys.stderr)
            return 1
        print("Frozen monitoring constants match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
