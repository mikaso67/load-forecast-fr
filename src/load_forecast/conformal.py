"""Rolling split-conformal 80% intervals around a fixed point forecast (M9 A, protocol 4.1).

For the delivery day D and a local half-hour slot, the interval is the point forecast plus the
lower and upper order statistics of the signed out-of-sample residuals (actual minus forecast) of
the last N local days known at the cutoff, that is the days D-2-N+1 to D-2 (D2: D-1 is never
read). Residuals of a slot are pooled across days, so a repeated autumn slot adds both of its
occurrences. Nothing is fitted: the point forecast is not changed.
"""

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

from load_forecast import evaluation as ev
from load_forecast.backtest import local_slots

UPDATE_LAG_DAYS = 2
LEVEL = 0.8
N_SLOTS = 48
EPSILON = 1e-9  # guards floor and ceil against float noise such as 30 * 0.1 = 3.0000000000000004


def order_statistic_ranks(n: int, level: float = LEVEL) -> tuple[int | None, int | None]:
    """1-based ranks of the lower and upper residual quantiles for n calibration residuals.

    Split conformal with rank floor((n+1)*a) below and ceil((n+1)*(1-a)) above, a = (1-level)/2:
    each tail misses with probability at most a under exchangeability. A rank outside 1..n means
    the window is too small for that tail: None, the interval is then left undefined (NaN).
    """
    tail = (1.0 - level) / 2.0
    low = math.floor((n + 1) * tail + EPSILON)
    high = math.ceil((n + 1) * (1.0 - tail) - EPSILON)
    return (low if low >= 1 else None), (high if high <= n else None)


def window_quantiles(residuals: np.ndarray, level: float = LEVEL) -> tuple[float, float]:
    """Lower and upper conformal residual quantiles of a window (NaN when it is too small)."""
    values = np.sort(residuals[np.isfinite(residuals)])
    low, high = order_statistic_ranks(len(values), level)
    return (values[low - 1] if low else np.nan), (values[high - 1] if high else np.nan)


def conformal_intervals(
    forecast: pd.Series, actual: pd.Series, *, window_days: int, level: float = LEVEL,
    update_lag_days: int = UPDATE_LAG_DAYS,
) -> pd.DataFrame:
    """Columns `lower` and `upper` on the UTC slots of `forecast`.

    Day D uses only the residuals of the local days D - update_lag_days - window_days + 1 to
    D - update_lag_days, read from `actual` row by row; nothing later is touched. Days with no
    usable slot in the window give NaN bounds.
    """
    if window_days < 1:
        raise ValueError('window_days must be at least 1')
    days = np.asarray(ev.local_dates(forecast.index))
    slots = np.asarray(local_slots(forecast.index))
    residual = (actual.reindex(forecast.index) - forecast).to_numpy(dtype=float)
    ordinal = np.array([d.toordinal() for d in days])
    lower = np.full(len(forecast), np.nan)
    upper = np.full(len(forecast), np.nan)
    by_slot = {s: np.flatnonzero(slots == s) for s in range(N_SLOTS)}
    for day in sorted(set(days)):
        last = day.toordinal() - update_lag_days
        first = last - window_days + 1
        today = np.flatnonzero(days == day)
        for slot, positions in by_slot.items():
            target = today[slots[today] == slot]
            if len(target) == 0:
                continue
            window = positions[(ordinal[positions] >= first) & (ordinal[positions] <= last)]
            low, high = window_quantiles(residual[window], level)
            lower[target] = forecast.to_numpy()[target] + low
            upper[target] = forecast.to_numpy()[target] + high
    return pd.DataFrame({'lower': lower, 'upper': upper}, index=forecast.index)


def seasonal_conformal_intervals(
    forecast: pd.Series, actual: pd.Series, *, window_days: int = 365, level: float = LEVEL,
    update_lag_days: int = UPDATE_LAG_DAYS,
) -> pd.DataFrame:
    """Seasonal group (Mondrian) split conformal per local slot (EXP-012).

    Columns `lower`, `upper` and `n_pool`. Same ranks as `conformal_intervals`, but the calibration
    pool of day D and slot s keeps only the residuals of slot s on days of the same meteorological
    season as D (Paris local date, `evaluation.get_season`) within the `window_days` days ending at
    D - update_lag_days. The season is a calendar attribute of D, known in advance; no observed
    quantity (temperature, day type label) defines the group. D-1 is never read. A day whose pool
    cannot support the nominal level is NaN, whatever the other seasons hold.
    """
    if window_days < 1:
        raise ValueError('window_days must be at least 1')
    days = np.asarray(ev.local_dates(forecast.index))
    slots = np.asarray(local_slots(forecast.index))
    point = forecast.to_numpy(dtype=float)
    residual = actual.reindex(forecast.index).to_numpy(dtype=float) - point
    ordinal = np.array([d.toordinal() for d in days])
    season = np.array([ev.get_season(d.month) for d in days])
    lower = np.full(len(forecast), np.nan)
    upper = np.full(len(forecast), np.nan)
    n_pool = np.zeros(len(forecast), dtype=int)
    by_slot = {s: np.flatnonzero(slots == s) for s in range(N_SLOTS)}
    for day in sorted(set(days)):
        last = day.toordinal() - update_lag_days
        first = last - window_days + 1
        today = np.flatnonzero(days == day)
        group = ev.get_season(day.month)
        for slot, positions in by_slot.items():
            target = today[slots[today] == slot]
            if len(target) == 0:
                continue
            pool = positions[(ordinal[positions] >= first) & (ordinal[positions] <= last)
                             & (season[positions] == group)]
            n_pool[target] = int(np.isfinite(residual[pool]).sum())
            low, high = window_quantiles(residual[pool], level)
            lower[target] = point[target] + low
            upper[target] = point[target] + high
    return pd.DataFrame({'lower': lower, 'upper': upper, 'n_pool': n_pool}, index=forecast.index)


def adaptive_ranks(n: int, alpha_t: float) -> tuple[int, int]:
    """1-based ranks of the lower and upper residual quantiles for the miscoverage level alpha_t.

    Same formula as `order_statistic_ranks` with the tail alpha_t / 2, but clipped to 1..n and with
    the tail kept in [0, 0.5], so a state outside [0, 1] gives the widest or the narrowest finite
    interval of the window instead of an infinite or an inverted one. The tail is clipped, the
    state alpha_t itself is not (Gibbs and Candes, 2021).
    """
    tail = min(max(alpha_t / 2.0, 0.0), 0.5)
    low = math.floor((n + 1) * tail + EPSILON)
    high = math.ceil((n + 1) * (1.0 - tail) - EPSILON)
    return max(low, 1), min(high, n)


def adaptive_conformal_intervals(
    forecast: pd.Series, actual: pd.Series, *, window_days: int, gamma: float, level: float = LEVEL,
    update_lag_days: int = UPDATE_LAG_DAYS,
) -> pd.DataFrame:
    """Rolling split conformal with adaptive miscoverage (ACI, Gibbs and Candes 2021), per local slot.

    Columns `lower`, `upper` and `alpha_t` (the state used for that day and slot). Each local slot
    keeps its own state, started at alpha = 1 - level and updated once per local day with
    alpha_{t+1} = alpha_t + gamma * (alpha - err_t), err_t being the share of the day's occurrences
    of the slot that fell outside the interval issued for that day (an autumn repeated slot has two,
    a missing actual has none). The interval of day D uses the window of `window_days` days ending
    at D - update_lag_days, as in `conformal_intervals`, and a state that has absorbed the errors of
    the days up to D - update_lag_days only: day D-1 is never read. A day is undefined (NaN, no
    update) when the window cannot support the nominal level. With gamma = 0 the result equals
    `conformal_intervals`.
    """
    if window_days < 1:
        raise ValueError('window_days must be at least 1')
    if gamma < 0:
        raise ValueError('gamma must not be negative')
    alpha = 1.0 - level
    days = np.asarray(ev.local_dates(forecast.index))
    slots = np.asarray(local_slots(forecast.index))
    point = forecast.to_numpy(dtype=float)
    observed = actual.reindex(forecast.index).to_numpy(dtype=float)
    residual = observed - point
    ordinal = np.array([d.toordinal() for d in days])
    lower = np.full(len(forecast), np.nan)
    upper = np.full(len(forecast), np.nan)
    state = np.full(len(forecast), np.nan)
    by_slot = {s: np.flatnonzero(slots == s) for s in range(N_SLOTS)}
    pending: dict[int, list[tuple[int, float]]] = {s: [] for s in range(N_SLOTS)}
    drift = dict.fromkeys(range(N_SLOTS), 0.0)
    for day in sorted(set(days)):
        last = day.toordinal() - update_lag_days
        first = last - window_days + 1
        today = np.flatnonzero(days == day)
        for slot, positions in by_slot.items():
            target = today[slots[today] == slot]
            if len(target) == 0:
                continue
            while pending[slot] and pending[slot][0][0] <= last:
                drift[slot] += alpha - pending[slot].pop(0)[1]
            alpha_t = alpha + gamma * drift[slot]
            window = positions[(ordinal[positions] >= first) & (ordinal[positions] <= last)]
            values = np.sort(residual[window][np.isfinite(residual[window])])
            if order_statistic_ranks(len(values), level)[0] is None:
                continue
            low_rank, high_rank = adaptive_ranks(len(values), alpha_t)
            lower[target] = point[target] + values[low_rank - 1]
            upper[target] = point[target] + values[high_rank - 1]
            state[target] = alpha_t
            known = np.isfinite(observed[target])
            if known.any():
                missed = (observed[target] < lower[target]) | (observed[target] > upper[target])
                pending[slot].append((day.toordinal(), float(missed[known].mean())))
    return pd.DataFrame({'lower': lower, 'upper': upper, 'alpha_t': state}, index=forecast.index)


def first_full_window_day(first_residual_day: date, window_days: int,
                          update_lag_days: int = UPDATE_LAG_DAYS) -> date:
    """First delivery day whose window of `window_days` days lies entirely on or after the first
    residual day, with residuals up to D - update_lag_days."""
    return first_residual_day + timedelta(days=window_days - 1 + update_lag_days)


# Scoring (protocol 4.1)


def pinball_loss(actual: np.ndarray, quantile_forecast: np.ndarray, tau: float) -> float:
    """Mean pinball loss at level tau."""
    error = actual - quantile_forecast
    return float(np.mean(np.maximum(tau * error, (tau - 1.0) * error)))


def scored_slots(intervals: pd.DataFrame, forecast: pd.Series, actual: pd.Series) -> pd.DataFrame:
    """Slots with an actual value and a defined interval, plus the covered flag."""
    frame = intervals.assign(point=forecast, actual=actual.reindex(intervals.index))
    frame = frame.dropna(subset=['lower', 'upper', 'point', 'actual'])
    frame['covered'] = (frame.actual >= frame.lower) & (frame.actual <= frame.upper)
    frame['width'] = frame.upper - frame.lower
    return frame


def coverage_bootstrap(frame: pd.DataFrame, block_days: int = ev.BLOCK_DAYS,
                       n_draws: int = ev.BOOTSTRAP_DRAWS, seed: int = ev.BOOTSTRAP_SEED) -> dict:
    """Slot-weighted coverage with a moving block bootstrap over local days.

    Each draw concatenates blocks of consecutive calendar days and divides the covered slots of
    the drawn days by their slots, so a 50-slot day weighs more than a 46-slot day. Days missing
    from the frame keep their place in the calendar as empty days.
    """
    dates = pd.DatetimeIndex(ev.local_dates(frame.index))
    calendar = pd.date_range(dates.min(), dates.max(), freq='D')
    covered = frame.covered.groupby(dates).sum().reindex(calendar, fill_value=0).to_numpy(float)
    count = frame.covered.groupby(dates).size().reindex(calendar, fill_value=0).to_numpy(float)
    n = len(calendar)
    if n < block_days:
        raise ValueError(f'Need at least {block_days} calendar days, got {n}')
    rng = np.random.default_rng(seed)
    n_blocks = -(-n // block_days)
    starts = rng.integers(0, n - block_days + 1, size=(n_draws, n_blocks))
    positions = (starts[:, :, None] + np.arange(block_days)).reshape(n_draws, -1)[:, :n]
    draws = covered[positions].sum(axis=1) / count[positions].sum(axis=1)
    low, high = np.percentile(draws, [2.5, 97.5])
    return {'coverage': float(covered.sum() / count.sum()), 'low': float(low), 'high': float(high),
            'n_slots': int(count.sum()), 'n_days': int((count > 0).sum()),
            'block_days': block_days, 'n_draws': n_draws, 'seed': seed}


def coverage_difference_bootstrap(a: pd.DataFrame, b: pd.DataFrame,
                                  block_days: int = ev.BLOCK_DAYS,
                                  n_draws: int = ev.BOOTSTRAP_DRAWS,
                                  seed: int = ev.BOOTSTRAP_SEED) -> dict:
    """Coverage of `a` minus coverage of `b` on the slots both score, with a moving block bootstrap.

    Both frames come from `scored_slots`. Only the slots present in both are used, so the two
    coverages are compared on the same timestamps; blocks of consecutive local days are drawn as in
    `coverage_bootstrap`, which pairs the two intervals by day.
    """
    common = a.index.intersection(b.index)
    a, b = a.loc[common], b.loc[common]
    dates = pd.DatetimeIndex(ev.local_dates(common))
    calendar = pd.date_range(dates.min(), dates.max(), freq='D')
    gain = (a.covered.astype(float) - b.covered.astype(float)).groupby(dates).sum()
    gain = gain.reindex(calendar, fill_value=0).to_numpy(float)
    count = a.covered.groupby(dates).size().reindex(calendar, fill_value=0).to_numpy(float)
    n = len(calendar)
    if n < block_days:
        raise ValueError(f'Need at least {block_days} calendar days, got {n}')
    rng = np.random.default_rng(seed)
    n_blocks = -(-n // block_days)
    starts = rng.integers(0, n - block_days + 1, size=(n_draws, n_blocks))
    positions = (starts[:, :, None] + np.arange(block_days)).reshape(n_draws, -1)[:, :n]
    draws = gain[positions].sum(axis=1) / count[positions].sum(axis=1)
    low, high = np.percentile(draws, [2.5, 97.5])
    return {'difference': float(gain.sum() / count.sum()), 'low': float(low), 'high': float(high),
            'n_slots': int(count.sum()), 'block_days': block_days, 'n_draws': n_draws, 'seed': seed}


def interval_report(frame: pd.DataFrame, cold: dict | None = None) -> dict:
    """Coverage with its bootstrap interval, width, pinball losses, by season and day type."""
    report = coverage_bootstrap(frame)
    report['mean_width_mw'] = float(frame.width.mean())
    report['pinball'] = {'0.1': pinball_loss(frame.actual.to_numpy(), frame.lower.to_numpy(), 0.1),
                         '0.5': pinball_loss(frame.actual.to_numpy(), frame.point.to_numpy(), 0.5),
                         '0.9': pinball_loss(frame.actual.to_numpy(), frame.upper.to_numpy(), 0.9)}
    dates = ev.local_dates(frame.index)
    seasons = pd.Series([ev.get_season(d.month) for d in dates], index=frame.index)
    report['by_season'] = {
        name: {'coverage': float(part.covered.mean()), 'mean_width_mw': float(part.width.mean()),
               'n_slots': int(len(part)), 'n_days': int(len(set(ev.local_dates(part.index))))}
        for name, part in frame.groupby(seasons)}
    labels = ev.label_days(pd.Index(sorted(set(dates))), cold)
    kinds = pd.Series(labels.reindex(dates).to_numpy(), index=frame.index)
    by_type = {name: part for name, part in frame.groupby(kinds)}
    by_type['cold_any'] = frame[[bool((cold or {}).get(d, False)) for d in dates]]
    report['by_day_type'] = {
        name: {'coverage': float(part.covered.mean()), 'mean_width_mw': float(part.width.mean()),
               'n_slots': int(len(part)), 'n_days': int(len(set(ev.local_dates(part.index))))}
        for name, part in by_type.items() if len(part)}
    return report
