"""Consolidated minus real-time consumption on shared half-hours, and its effect on the lags.

The gap is `consolidated - real_time` on the same UTC half-hour. A production lag reads the real-time
value where training read the consolidated one, so the production lag feature moves by `-gap` at
the lag timestamp. Inference uses daily means on Paris dates and the moving block bootstrap of
`evaluation.py` (D10), because half-hours of one day are strongly correlated.
"""

from datetime import date

import numpy as np
import pandas as pd

from load_forecast import evaluation as ev
from load_forecast.backtest import local_slots
from load_forecast.cutoff import delivery_slots
from load_forecast.lag_model import LAG_COLUMNS, lag_features


def _check_gap(gap: pd.Series) -> None:
    if not isinstance(gap.index, pd.DatetimeIndex) or str(gap.index.tz) != 'UTC':
        raise ValueError('Expected a UTC-indexed gap series')
    if gap.index.has_duplicates or gap.isna().any():
        raise ValueError('Gap series must be unique and complete on its index')


def daily_means(values: pd.Series) -> pd.Series:
    """Mean per Paris date on the full calendar between the first and last date, gaps as NaN."""
    days = pd.DatetimeIndex(ev.local_dates(values.index))
    daily = values.groupby(days).mean()
    return daily.reindex(pd.date_range(daily.index.min(), daily.index.max(), freq='D'))


def summarize(gap: pd.Series) -> dict:
    """Slot statistics plus 7-day (primary) and 14-day (check) bootstrap intervals of the mean."""
    _check_gap(gap)
    daily = daily_means(gap)
    primary = ev.block_bootstrap(daily, ev.BLOCK_DAYS)
    check = ev.block_bootstrap(daily, ev.CHECK_BLOCK_DAYS)
    return {
        'n_slots': int(len(gap)), 'n_days': primary.n_days,
        'mean': float(gap.mean()), 'median': float(gap.median()),
        'sd': float(gap.std(ddof=1)), 'mean_abs': float(gap.abs().mean()),
        'mean_daily_mean': primary.mean,
        'ci7_low': primary.low, 'ci7_high': primary.high,
        'ci14_low': check.low, 'ci14_high': check.high,
    }


def summarize_by(gap: pd.Series, by: str) -> pd.DataFrame:
    """One `summarize` row per Paris month ('month') or Paris clock hour ('hour')."""
    _check_gap(gap)
    local = gap.index.tz_convert('Europe/Paris')
    if by == 'month':
        keys = pd.Index(local.strftime('%Y-%m'))
    elif by == 'hour':
        keys = pd.Index(local.hour)
    else:
        raise ValueError("by must be 'month' or 'hour'")
    rows = {key: summarize(gap[keys == key]) for key in sorted(set(keys))}
    return pd.DataFrame.from_dict(rows, orient='index').rename_axis(by)


def lag_shifts(gap: pd.Series, days: list[date]) -> pd.DataFrame:
    """Production minus backtest value of each lag feature, per delivery slot.

    Uses the lag rules of the served model unchanged. A day with any lag timestamp outside the
    overlap is dropped, as the missing-lag rule would drop it.
    """
    _check_gap(gap)
    shift = -gap
    frames = []
    for day in days:
        slots = pd.DatetimeIndex(delivery_slots(day, '30min'))
        features = lag_features(slots, shift, day)[LAG_COLUMNS]
        if features.notna().all().all():
            frames.append(features)
    if not frames:
        raise ValueError('No delivery day has all its lags inside the overlap')
    return pd.concat(frames)


def forecast_shift(shifts: pd.DataFrame, coefficients: pd.DataFrame) -> pd.Series:
    """Linear forecast shift sum_k beta_k(slot) * shift_k, with one coefficient row per local slot."""
    if list(coefficients.columns) != LAG_COLUMNS or len(coefficients) != 48:
        raise ValueError('Expected 48 local slots by the three lag coefficients')
    slot_ids = np.asarray(local_slots(shifts.index))
    beta = coefficients.to_numpy(dtype=float)[slot_ids]
    return pd.Series((shifts[LAG_COLUMNS].to_numpy(dtype=float) * beta).sum(axis=1),
                     index=shifts.index, name='forecast_shift')
