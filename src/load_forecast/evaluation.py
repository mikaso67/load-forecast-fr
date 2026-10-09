"""Evaluation harness from docs/protocol.md: metrics, day types, paired bootstrap, naive baseline.

Pure functions, no database access. A scored frame is indexed by aware UTC slot starts and has the
columns `actual`, `model` and `rte_d1` (MW).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import pandas as pd

from load_forecast.cutoff import PARIS, available_at, forecast_cutoff

ACTUAL = "actual"
MODEL = "model"
RTE = "rte_d1"

CONSUMPTION_DELAY = timedelta(hours=2)  # D2: last usable slot ends at or before cutoff - 2 h
BOOTSTRAP_SEED = 20240
BOOTSTRAP_DRAWS = 10_000
BLOCK_DAYS = 7
CHECK_BLOCK_DAYS = 14

DAY_TYPES = ("ordinary", "cold", "holiday", "bridge", "dst")


@dataclass(frozen=True)
class Metrics:
    mae: float
    rmse: float
    mape: float
    n_slots: int


@dataclass(frozen=True)
class Scores:
    """Model and RTE D-1 scored on the same slots, with the slots that could not be scored."""

    model: Metrics
    rte: Metrics
    n_excluded: int
    excluded: pd.DatetimeIndex


@dataclass(frozen=True)
class Bootstrap:
    mean: float
    low: float
    high: float
    n_days: int
    block_days: int
    n_draws: int
    seed: int
    n_missing: int = 0

    @property
    def significant(self) -> bool:
        return not self.low <= 0.0 <= self.high

    @property
    def verdict(self) -> str:
        return "significant" if self.significant else "not significant"


# 1. Metrics on common slots


def _check_utc_index(index: pd.Index) -> None:
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise ValueError("Expected a timezone-aware DatetimeIndex")
    if str(index.tz) != "UTC":
        raise ValueError("Expected a UTC index; convert to UTC before scoring")


def _metrics(actual: pd.Series, forecast: pd.Series) -> Metrics:
    error = (actual - forecast).to_numpy(dtype=float)
    y = actual.to_numpy(dtype=float)
    if len(y) == 0:
        return Metrics(np.nan, np.nan, np.nan, 0)
    if (y <= 0).any():
        raise ValueError("MAPE needs strictly positive actual values")
    return Metrics(
        mae=float(np.mean(np.abs(error))),
        rmse=float(np.sqrt(np.mean(error**2))),
        mape=float(100.0 * np.mean(np.abs(error) / y)),
        n_slots=len(y),
    )


def common_slots(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep the slots where the actual value, the model and RTE D-1 are all present."""
    _check_utc_index(frame.index)
    if frame.index.has_duplicates:
        raise ValueError("Duplicate slot timestamps in the scored frame")
    return frame[[ACTUAL, MODEL, RTE]].dropna()


def score(frame: pd.DataFrame) -> Scores:
    """MAE, RMSE, MAPE of the model and of RTE D-1 on exactly the same slots."""
    kept = common_slots(frame)
    excluded = frame.index.difference(kept.index)
    return Scores(
        model=_metrics(kept[ACTUAL], kept[MODEL]),
        rte=_metrics(kept[ACTUAL], kept[RTE]),
        n_excluded=len(excluded),
        excluded=excluded,
    )


def metrics(actual: pd.Series, forecast: pd.Series) -> Metrics:
    """Compute MAE, RMSE, and MAPE on common non-null slots of two series."""
    if not isinstance(actual, pd.Series) or not isinstance(forecast, pd.Series):
        raise TypeError("Expected pandas Series for actual and forecast")
    _check_utc_index(actual.index)
    _check_utc_index(forecast.index)
    if actual.index.has_duplicates or forecast.index.has_duplicates:
        raise ValueError("Series indexes must not contain duplicate timestamps")
    if not actual.index.equals(forecast.index):
        common = actual.index.intersection(forecast.index)
        actual = actual.loc[common]
        forecast = forecast.loc[common]
    valid = actual.notna() & forecast.notna()
    return _metrics(actual[valid], forecast[valid])


def local_dates(index: pd.DatetimeIndex) -> pd.Index:
    """Europe/Paris calendar date of each UTC slot start."""
    _check_utc_index(index)
    return pd.Index(index.tz_convert(PARIS).date, name="date")


def daily_mae(frame: pd.DataFrame) -> pd.DataFrame:
    """Daily MAE per local day of the model and RTE D-1, on common slots, with the slot count."""
    kept = common_slots(frame)
    dates = local_dates(kept.index)
    out = pd.DataFrame(
        {
            MODEL: (kept[ACTUAL] - kept[MODEL]).abs().to_numpy(),
            RTE: (kept[ACTUAL] - kept[RTE]).abs().to_numpy(),
        },
        index=dates,
    )
    grouped = out.groupby(level=0)
    result = grouped.mean()
    result["n_slots"] = grouped.size()
    return result


# 2. Day types (protocol section 5)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def holidays(year: int) -> frozenset[date]:
    """The 11 national public holidays (D7). Regional Alsace-Moselle days are ignored."""
    easter = _easter(year)
    return frozenset(
        {
            date(year, 1, 1),
            easter + timedelta(days=1),
            date(year, 5, 1),
            date(year, 5, 8),
            easter + timedelta(days=39),
            easter + timedelta(days=50),
            date(year, 7, 14),
            date(year, 8, 15),
            date(year, 11, 1),
            date(year, 11, 11),
            date(year, 12, 25),
        }
    )


def is_holiday(day: date) -> bool:
    return day in holidays(day.year)


def is_bridge(day: date) -> bool:
    """D8: Monday before a Tuesday holiday, or Friday after a Thursday holiday."""
    if day.weekday() == 0:
        return is_holiday(day + timedelta(days=1))
    if day.weekday() == 4:
        return is_holiday(day - timedelta(days=1))
    return False


def local_day_hours(day: date) -> float:
    """Length of the Europe/Paris local day in hours: 23, 24 or 25."""
    start = datetime.combine(day, time(), PARIS).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(), PARIS).astimezone(UTC)
    return (end - start) / timedelta(hours=1)


def is_dst_day(day: date) -> bool:
    return local_day_hours(day) != 24


def get_season(month: int) -> str:
    """Return meteorological season for a given calendar month (1 to 12)."""
    if month not in range(1, 13):
        raise ValueError(f"Month must be between 1 and 12, got {month}")
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "autumn"


def is_cold(daily_temperature: float, threshold: float) -> bool:
    """D9 flag. The threshold is a parameter: its value is fixed once, outside this module."""
    return bool(daily_temperature < threshold)


def day_type(day: date, cold: bool = False) -> str:
    """One label per day, precedence DST > holiday > bridge > cold > ordinary."""
    if is_dst_day(day):
        return "dst"
    if is_holiday(day):
        return "holiday"
    if is_bridge(day):
        return "bridge"
    return "cold" if cold else "ordinary"


def label_days(days: Iterable[date], cold: Mapping[date, bool] | None = None) -> pd.Series:
    """Day-type label of each local date. Days missing from `cold` count as not cold."""
    cold = cold or {}
    days = list(days)
    return pd.Series(
        [day_type(d, cold.get(d, False)) for d in days], index=pd.Index(days, name="date")
    )


def mae_by_day_type(frame: pd.DataFrame, cold: Mapping[date, bool] | None = None) -> pd.DataFrame:
    """Slot-weighted MAE by day type, for the model and RTE D-1 (protocol section 4).

    Each row is the sum of absolute errors over all common slots of the days of that type, divided
    by their number: a 50-slot day weighs more than a 46-slot day. Rows follow the exclusive
    precedence of the protocol, plus `cold_any`: the cold flag on all days, so a cold holiday still
    counts there. `n_days` counts the days with at least one common slot. The daily MAE of
    `daily_mae` is kept for the paired daily differences and the bootstrap only.
    """
    kept = common_slots(frame)
    dates = local_dates(kept.index)
    errors = pd.DataFrame(
        {
            MODEL: (kept[ACTUAL] - kept[MODEL]).abs().to_numpy(),
            RTE: (kept[ACTUAL] - kept[RTE]).abs().to_numpy(),
        },
        index=dates,
    )
    labels = label_days(pd.Index(dates.unique()), cold)
    slot_labels = labels.reindex(dates).to_numpy()
    slot_cold = np.array([bool((cold or {}).get(d, False)) for d in dates], dtype=bool)
    masks = {name: slot_labels == name for name in DAY_TYPES}
    masks["cold_any"] = slot_cold
    table = pd.DataFrame(
        {
            name: {
                MODEL: errors[MODEL][mask].mean(),
                RTE: errors[RTE][mask].mean(),
                "n_days": errors.index[mask].nunique(),
                "n_slots": int(mask.sum()),
            }
            for name, mask in masks.items()
        }
    ).T
    table[["n_days", "n_slots"]] = table[["n_days", "n_slots"]].astype(int)
    return table


# 3. Paired block bootstrap (D10)


def _consecutive_days(index: pd.Index) -> None:
    days = pd.DatetimeIndex(index)
    if days.tz is not None:
        raise ValueError("Daily differences must be indexed by local calendar dates, not instants")
    if days.has_duplicates or not days.is_monotonic_increasing:
        raise ValueError("Daily index must be unique and sorted")
    if not (days.normalize() == days).all():
        raise ValueError("Daily index must hold whole dates")
    if len(days) > 1 and not (days[1:] - days[:-1] == pd.Timedelta(days=1)).all():
        raise ValueError("Daily index has gaps: reindex on the full calendar, missing days as NaN")


def block_bootstrap(
    daily_diff: pd.Series | np.ndarray,
    block_days: int = BLOCK_DAYS,
    n_draws: int = BOOTSTRAP_DRAWS,
    seed: int = BOOTSTRAP_SEED,
) -> Bootstrap:
    """Moving block bootstrap of the mean of paired daily differences, percentile 95% interval.

    The input covers a calendar of consecutive days, one entry per day. A missing day is NaN: it
    keeps its place, so its neighbours are never treated as adjacent. Blocks of `block_days`
    consecutive calendar days are drawn from every possible start and concatenated until the
    calendar length is reached; each draw averages its non-NaN days. A Series must be indexed by
    unique, sorted, consecutive dates (otherwise ValueError); an array is taken as consecutive days.
    """
    if isinstance(daily_diff, pd.Series):
        _consecutive_days(daily_diff.index)
    d = np.asarray(daily_diff, dtype=float)
    n = len(d)
    valid = ~np.isnan(d)
    n_valid = int(valid.sum())
    if block_days < 1 or n < block_days:
        raise ValueError(f"Need at least {block_days} calendar days, got {n}")
    if n_valid < 2:
        raise ValueError(f"Need at least 2 non-missing days, got {n_valid}")
    rng = np.random.default_rng(seed)
    n_blocks = -(-n // block_days)
    starts = rng.integers(0, n - block_days + 1, size=(n_draws, n_blocks))
    positions = (starts[:, :, None] + np.arange(block_days)).reshape(n_draws, -1)[:, :n]
    counts = valid[positions].sum(axis=1)
    sums = np.where(valid, d, 0.0)[positions].sum(axis=1)
    means = sums[counts > 0] / counts[counts > 0]
    low, high = np.percentile(means, [2.5, 97.5])
    return Bootstrap(
        mean=float(d[valid].mean()),
        low=float(low),
        high=float(high),
        n_days=n_valid,
        block_days=block_days,
        n_draws=n_draws,
        seed=seed,
        n_missing=n - n_valid,
    )


def _by_day(daily: pd.Series) -> pd.Series:
    index = pd.DatetimeIndex(daily.index)
    if index.has_duplicates:
        raise ValueError("Duplicate days in a daily series")
    return pd.Series(daily.to_numpy(dtype=float), index=index).sort_index()


def compare_daily(
    model_daily: pd.Series,
    reference_daily: pd.Series,
    block_days: int = BLOCK_DAYS,
    first: date | None = None,
    last: date | None = None,
) -> Bootstrap:
    """Paired comparison, difference is model minus reference, on the full calendar of days.

    The calendar runs from `first` to `last` (default: the earliest and latest day of either
    series). A day missing in either series stays in the calendar as NaN and is counted in
    `n_missing`; it is never dropped, so blocks always span consecutive calendar days.
    """
    model_daily = _by_day(model_daily)
    reference_daily = _by_day(reference_daily)
    days = model_daily.index.union(reference_daily.index)
    lo = pd.Timestamp(first) if first is not None else days.min()
    hi = pd.Timestamp(last) if last is not None else days.max()
    calendar = pd.date_range(lo, hi, freq="D")
    diff = model_daily.reindex(calendar) - reference_daily.reindex(calendar)
    return block_bootstrap(diff, block_days=block_days)


def compare_with_check(
    model_daily: pd.Series,
    reference_daily: pd.Series,
    first: date | None = None,
    last: date | None = None,
) -> tuple[Bootstrap, Bootstrap]:
    """The 7-day result and the 14-day sensitivity check of D10."""
    return (
        compare_daily(model_daily, reference_daily, BLOCK_DAYS, first, last),
        compare_daily(model_daily, reference_daily, CHECK_BLOCK_DAYS, first, last),
    )


# 4. Seasonal naive baseline


def last_usable_slot_start(target_day: date, freq: str = "30min") -> datetime:
    """Latest slot start whose slot ends at or before cutoff - 2 h (D2)."""
    limit = forecast_cutoff(target_day) - CONSUMPTION_DELAY
    return limit - pd.Timedelta(freq)


def seasonal_naive(
    history: pd.Series,
    target_day: date,
    freq: str = "30min",
    lag_days: int = 7,
    published_at: pd.Series | None = None,
) -> pd.Series:
    """Forecast of each slot of `target_day`: the actual of the same UTC slot `lag_days` earlier.

    The history is first cut at the last usable slot of D2, so a lag that points after it gives
    NaN instead of a leaked value. A missing source slot also gives NaN: nothing is filled.

    `published_at` (optional) holds the aware publication time of each history value, indexed like
    `history`. When given, a value is also required to be published strictly before the cutoff
    (`cutoff.available_at`); a value with no publication time is dropped. It can only remove
    values, never add them. Without it, only the D2 slot-end rule applies.

    Limitation: the backtest history is the consolidated series, whose true publication dates are
    not stored per value, so D2 is the default proxy. Production sees real-time values, which can
    differ slightly from consolidated ones (accepted leak, protocol checklist item 12).
    """
    _check_utc_index(history.index)
    usable = history.loc[history.index <= last_usable_slot_start(target_day, freq)]
    if published_at is not None:
        published = published_at.reindex(usable.index)
        if not isinstance(published.dtype, pd.DatetimeTZDtype):
            raise ValueError("published_at must hold timezone-aware timestamps")
        table = pd.DataFrame({"value": usable, "published_at": published})
        usable = available_at(table, forecast_cutoff(target_day))["value"]
    start = datetime.combine(target_day, time(), PARIS).astimezone(UTC)
    end = datetime.combine(target_day + timedelta(days=1), time(), PARIS).astimezone(UTC)
    slots = pd.date_range(start, end, freq=freq, inclusive="left")
    source = slots - timedelta(days=lag_days)
    values = usable.reindex(source).to_numpy(dtype=float)
    return pd.Series(values, index=slots, name="seasonal_naive")
