"""Production scoring and drift monitoring (docs/production_design.md sections 4.2 and 5).

Pure functions, no database access. Scoring compares the served forecast, the primary model,
raw RTE D-1 and RTE* on the same slots of one delivery day. Drift monitoring is a two-sided
CUSUM on the standardized daily mean error, with its threshold calibrated by a seasonal block
bootstrap of the reference errors (docs/monitoring.md).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from load_forecast.evaluation import get_season

VINTAGES = ("realtime", "consolidated", "definitive")
MIN_SLOTS = 40
MAX_MISSING_RUN = 7
K = 0.5
SKILL_WINDOW = 28
SKILL_MIN_DAYS = 21
CALENDAR_WINDOW = 15
YEAR_DAYS = 365

# Frozen before go-live by scripts/calibrate_monitoring.py on the candidate 1 daily errors of
# 2024 and 2025 (actual minus forecast, MW, days with at least MIN_SLOTS scored slots).
SEASON_MEAN = {"autumn": 46.8662, "spring": -133.0232, "summer": 94.3143, "winter": 7.0584}
SEASON_STD = {"autumn": 711.7041, "spring": 834.5233, "summer": 644.4702, "winter": 1426.6854}
H = 11.46


# 1. Scoring


def _forecast_metrics(actual: pd.Series, forecast: pd.Series) -> dict[str, float | int]:
    error = forecast - actual
    return {
        "n_slots": int(len(actual)),
        "mae": float(error.abs().mean()),
        "rmse": float(np.sqrt((error**2).mean())),
        "mape": float((error.abs() / actual.abs()).mean() * 100.0),
        "mean_error": float(error.mean()),
    }


def _interval_metrics(actual: pd.Series, lower: pd.Series | None,
                      upper: pd.Series | None) -> dict[str, float | None]:
    if lower is None or upper is None or lower.isna().any() or upper.isna().any():
        return {"coverage": None, "width": None}
    covered = (actual >= lower) & (actual <= upper)
    return {"coverage": float(covered.mean()), "width": float((upper - lower).mean())}


def score_day(
    actual: pd.Series,
    rte: pd.Series,
    served: pd.DataFrame | None,
    model: pd.DataFrame | None,
    rte_star: pd.Series | None,
    vintage: str,
) -> dict:
    """Score one delivery day on the slots where every available forecast and the actual exist.

    `served` and `model` hold the columns `point`, `lower`, `upper` (lower and upper may be NaN,
    as on fallback days). `served` is None on a day without delivery and `model` is None when the
    day has no state run. RTE* is only scored on consolidated or definitive actuals, because its
    correction was estimated on that scale; on real-time actuals it must be None. A RTE* that is
    None or entirely NaN is reported as missing. Errors are forecast minus actual (MW).
    """
    if vintage not in VINTAGES:
        raise ValueError(f"Unknown actual vintage {vintage!r}")
    if vintage == "realtime" and rte_star is not None:
        raise ValueError("RTE* is not defined on real-time actuals")
    if rte_star is not None and rte_star.isna().all():
        rte_star = None
    columns = {"actual": actual, "rte_d1": rte}
    for name, frame in (("served", served), ("model", model)):
        if frame is not None:
            columns[name] = frame["point"]
    if rte_star is not None:
        columns["rte_star"] = rte_star
    frame = pd.DataFrame(columns)
    if frame.index.has_duplicates or str(getattr(frame.index, "tz", None)) != "UTC":
        raise ValueError("Expected unique UTC slot timestamps")
    kept = frame.dropna()
    result: dict = {"vintage": vintage, "n_common_slots": int(len(kept))}
    for name in ("served", "model", "rte_d1", "rte_star"):
        if name not in kept or kept.empty:
            result[name] = None
            continue
        metrics = _forecast_metrics(kept["actual"], kept[name])
        source = {"served": served, "model": model}.get(name)
        if source is not None:
            bounds = source.reindex(kept.index)
            metrics |= _interval_metrics(kept["actual"], bounds["lower"], bounds["upper"])
        result[name] = metrics
    return result


def score_row(day: date, choice_hash: str, metrics: Mapping, actual_max_ingested_at: str,
              scored_at: str) -> dict:
    """One `forecast.scores` row: a new row per newer actual vintage, never an update."""
    return {
        "delivery_day": day.isoformat(),
        "choice_hash": choice_hash,
        "actual_vintage": metrics["vintage"],
        "actual_max_ingested_at": actual_max_ingested_at,
        "scored_at": scored_at,
        "metrics": dict(metrics),
    }


def daily_error(metrics: Mapping) -> float:
    """CUSUM input e_d: mean of actual minus served forecast, NaN for a missing day."""
    served = metrics.get("served")
    if served is None or served["n_slots"] < MIN_SLOTS:
        return float("nan")
    return -served["mean_error"]


# 2. CUSUM on the standardized daily mean error


def season_of(day: date) -> str:
    return get_season(day.month)


def standardize(errors: pd.Series, mean: Mapping[str, float] = SEASON_MEAN,
                std: Mapping[str, float] = SEASON_STD) -> pd.Series:
    """z_d = (e_d - mu_s) / sigma_s with s the season of the Paris delivery day d."""
    seasons = [season_of(pd.Timestamp(day).date()) for day in errors.index]
    mu = np.array([mean[s] for s in seasons])
    sigma = np.array([std[s] for s in seasons])
    return pd.Series((errors.to_numpy(dtype=float) - mu) / sigma, index=errors.index, name="z")


def _check_calendar(index: pd.Index) -> None:
    days = pd.DatetimeIndex(index)
    if days.tz is not None or days.has_duplicates or not days.is_monotonic_increasing:
        raise ValueError("CUSUM input must be indexed by sorted, unique Paris calendar dates")
    if len(days) > 1 and not (days[1:] - days[:-1] == pd.Timedelta(days=1)).all():
        raise ValueError("CUSUM input has gaps: reindex on the full calendar, missing days as NaN")


def cusum(z: pd.Series, h: float = H, k: float = K) -> pd.DataFrame:
    """Two-sided CUSUM of section 5 P7, one row per calendar day.

    Both sums start from 0 before the first scored day. A NaN day is missing: the sums carry over
    unchanged, no alarm is possible, and it is counted. When the missing run exceeds
    MAX_MISSING_RUN days, both sums restart at 0 (event `restart_missing`). After an alarm both
    sums restart at 0 on the next day: the alarm day shows the value that crossed h.
    """
    _check_calendar(z.index)
    s_plus = s_minus = 0.0
    missing_run = 0
    restart_next = False
    rows = []
    for value in z.to_numpy(dtype=float):
        event = ""
        if restart_next:
            s_plus = s_minus = 0.0
            restart_next = False
        if np.isnan(value):
            missing_run += 1
            if missing_run == MAX_MISSING_RUN + 1:
                s_plus = s_minus = 0.0
                event = "restart_missing"
            rows.append((s_plus, s_minus, False, True, missing_run, event))
            continue
        missing_run = 0
        s_plus = max(0.0, s_plus + value - k)
        s_minus = max(0.0, s_minus - value - k)
        alarm = s_plus > h or s_minus > h
        if alarm:
            event = "alarm_up" if s_plus > h else "alarm_down"
            restart_next = True
        rows.append((s_plus, s_minus, alarm, False, 0, event))
    return pd.DataFrame(rows, index=z.index,
                        columns=["s_plus", "s_minus", "alarm", "missing", "missing_run", "event"])


def relative_skill(served_mae: pd.Series, rte_mae: pd.Series, window: int = SKILL_WINDOW,
                   min_days: int = SKILL_MIN_DAYS) -> pd.DataFrame:
    """Item 2 band: trailing mean of the daily MAE difference served minus raw RTE D-1.

    Both series hold daily MAE on the same slots and the same actual vintage. The window covers
    `window` calendar days ending on each day; it alerts when its mean is above 0 and it holds at
    least `min_days` paired days.
    """
    difference = served_mae - rte_mae
    _check_calendar(difference.index)
    rolling = difference.rolling(window, min_periods=1)
    out = pd.DataFrame({"mean_difference": rolling.mean(), "n_days": rolling.count()})
    out["alert"] = (out["n_days"] >= min_days) & (out["mean_difference"] > 0)
    return out


# 3. Calibration of h by seasonal calendar-position block bootstrap


def calendar_positions(days: pd.Index) -> np.ndarray:
    """Position 0..364 of each date in a 365-day year; 29 February shares 28 February's place."""
    stamps = pd.DatetimeIndex(days)
    after_28_february = (stamps.month > 2) | ((stamps.month == 2) & (stamps.day == 29))
    leap_shift = (stamps.is_leap_year & after_28_february).astype(int)
    return np.asarray(stamps.dayofyear - 1 - leap_shift)


def _circular_distance(a: np.ndarray, b: int) -> np.ndarray:
    gap = np.abs(a - b) % YEAR_DAYS
    return np.minimum(gap, YEAR_DAYS - gap)


@dataclass(frozen=True)
class BlockSampler:
    """Draws blocks of consecutive reference days that start near a target calendar position."""

    z: np.ndarray
    block_days: int
    candidates: tuple[np.ndarray, ...]

    @classmethod
    def build(cls, z: pd.Series, block_days: int,
              window: int = CALENDAR_WINDOW) -> "BlockSampler":
        _check_calendar(z.index)
        if z.isna().any():
            raise ValueError("Reference series must have no missing day")
        positions = calendar_positions(z.index)
        starts = np.arange(len(z) - block_days + 1)
        candidates = tuple(starts[_circular_distance(positions[starts], target) <= window]
                           for target in range(YEAR_DAYS))
        if any(len(c) == 0 for c in candidates):
            raise ValueError("Some calendar position has no block start in the reference")
        return cls(z.to_numpy(dtype=float), block_days, candidates)

    def years(self, n_years: int, rng: np.random.Generator, days: int = YEAR_DAYS) -> np.ndarray:
        """n_years simulated series of `days` days, each starting at a uniform calendar position."""
        n_blocks = -(-days // self.block_days)
        first = rng.integers(0, YEAR_DAYS, size=n_years)
        out = np.empty((n_years, n_blocks * self.block_days))
        offsets = np.arange(self.block_days)
        for block in range(n_blocks):
            targets = (first + block * self.block_days) % YEAR_DAYS
            starts = np.empty(n_years, dtype=int)
            for target in np.unique(targets):
                rows = targets == target
                starts[rows] = rng.choice(self.candidates[target], size=int(rows.sum()))
            columns = slice(block * self.block_days, (block + 1) * self.block_days)
            out[:, columns] = self.z[starts[:, None] + offsets]
        return out[:, :days]


def cusum_maxima(paths: np.ndarray, k: float = K) -> np.ndarray:
    """Largest of S+ and S- over each simulated path, with no reset.

    Before the first alarm the reset rule never applies, so P(at least one alarm) = P(max > h).
    """
    s_plus = np.zeros(paths.shape[0])
    s_minus = np.zeros(paths.shape[0])
    peak = np.zeros(paths.shape[0])
    for column in paths.T:
        s_plus = np.maximum(0.0, s_plus + column - k)
        s_minus = np.maximum(0.0, s_minus - column - k)
        peak = np.maximum(peak, np.maximum(s_plus, s_minus))
    return peak


def first_alarm(paths: np.ndarray, h: float, k: float = K) -> np.ndarray:
    """1-based day of the first alarm of each path, 0 when no alarm occurs."""
    s_plus = np.zeros(paths.shape[0])
    s_minus = np.zeros(paths.shape[0])
    found = np.zeros(paths.shape[0], dtype=int)
    for day, column in enumerate(paths.T, start=1):
        s_plus = np.maximum(0.0, s_plus + column - k)
        s_minus = np.maximum(0.0, s_minus - column - k)
        new = (found == 0) & ((s_plus > h) | (s_minus > h))
        found[new] = day
    return found


def smallest_h(maxima: np.ndarray, target: float, step: float = 0.01) -> float:
    """Smallest h on a `step` grid with simulated P(max > h) at most `target`."""
    units = int(np.ceil(np.quantile(maxima, 1.0 - target, method="higher") / step))
    while units > 0 and np.mean(maxima > (units - 1) * step) <= target:
        units -= 1
    return float(round(units * step, 10))


def autocorrelation(z: pd.Series, max_lag: int) -> np.ndarray:
    """Sample autocorrelation at lags 0..max_lag, biased estimator (divides by n)."""
    x = z.to_numpy(dtype=float) - z.mean()
    n = len(x)
    gamma = np.array([np.dot(x[: n - lag], x[lag:]) / n for lag in range(max_lag + 1)])
    return gamma / gamma[0]


def bartlett_variance(z: pd.Series, bandwidth: int) -> float:
    """Bartlett (Newey-West) long-run variance with weights 1 - j / bandwidth, j < bandwidth.

    With bandwidth b this is also, to first order, the variance of the mean (times n) that a
    moving block bootstrap with b-day blocks retains.
    """
    x = z.to_numpy(dtype=float) - z.mean()
    n = len(x)
    total = np.dot(x, x) / n
    for lag in range(1, bandwidth):
        total += 2.0 * (1.0 - lag / bandwidth) * np.dot(x[: n - lag], x[lag:]) / n
    return float(total)
