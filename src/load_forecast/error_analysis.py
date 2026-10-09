"""Error analysis of saved forecasts: daily tables, hour and month profiles, holidays, temperature.

Pure functions on a frame with UTC-indexed half-hour slots and the columns `model`, `actual`,
`rte_d1`, `rte_star`, plus `lower`, `upper`, `covered`, `n_pool` for the interval. Nothing here
fits a model or reads a database.
"""

from collections.abc import Mapping
from datetime import date, timedelta

import numpy as np
import pandas as pd

from load_forecast import evaluation as ev
from load_forecast.conformal import order_statistic_ranks, window_quantiles

FORECASTS = ('model', 'rte_d1', 'rte_star')
HOLIDAY_NAMES = {(1, 1): 'New Year', (5, 1): 'Labour Day', (5, 8): 'Victory Day',
                 (7, 14): 'Bastille Day', (8, 15): 'Assumption', (11, 1): 'All Saints',
                 (11, 11): 'Armistice', (12, 25): 'Christmas'}


def common(frame: pd.DataFrame) -> pd.DataFrame:
    """Slots where actual, the model and both references are finite: the scoring set of the project."""
    return frame.loc[np.isfinite(frame[['actual', *FORECASTS]]).all(axis=1)]


def holiday_name(day: date) -> str:
    """Name of a national holiday of D7, or an empty string."""
    if not ev.is_holiday(day):
        return ''
    if (day.month, day.day) in HOLIDAY_NAMES:
        return HOLIDAY_NAMES[(day.month, day.day)]
    moveable = sorted(d for d in ev.holidays(day.year) if (d.month, d.day) not in HOLIDAY_NAMES)
    return ('Easter Monday', 'Ascension', 'Whit Monday')[moveable.index(day)]


def daily_table(frame: pd.DataFrame, cold: Mapping[date, bool],
                temperature: Mapping[date, float]) -> pd.DataFrame:
    """One row per local day on the common slots: errors, interval coverage, day type, temperature.

    `bias` is the mean signed error (forecast minus actual) of the model, `model`, `rte_d1` and
    `rte_star` are daily MAE (MW). `type_d2` is the day type of the D-2 lag day, which feeds the
    lag features of the point models.
    """
    kept = common(frame)
    days = pd.Index(ev.local_dates(kept.index), name='date')
    error = kept[list(FORECASTS)].sub(kept.actual, axis=0)
    table = error.abs().set_axis(days).groupby(level=0).mean()
    table['bias'] = error.model.set_axis(days).groupby(level=0).mean()
    table['actual'] = kept.actual.set_axis(days).groupby(level=0).mean()
    table['coverage'] = kept.covered.astype(float).set_axis(days).groupby(level=0).mean()
    table['width'] = kept.width.set_axis(days).groupby(level=0).mean()
    # The interval bounds in error coordinates (forecast minus actual): the actual lies between
    # lower and upper, so the signed error lies between model - upper and model - lower.
    table['band_low'] = (kept.model - kept.upper).set_axis(days).groupby(level=0).mean()
    table['band_high'] = (kept.model - kept.lower).set_axis(days).groupby(level=0).mean()
    table['slots'] = kept.model.set_axis(days).groupby(level=0).size()
    index = table.index
    table['type'] = ev.label_days(index, cold).to_numpy()
    table['weekday'] = [d.strftime('%a') for d in index]
    table['temperature'] = [temperature.get(d, np.nan) for d in index]
    table['holiday'] = [holiday_name(d) for d in index]
    table['actual_d7'] = table.actual.reindex([d - timedelta(days=7) for d in index]).to_numpy()
    table['type_d2'] = ev.label_days([d - timedelta(days=2) for d in index], cold).to_numpy()
    return table


def worst_days(daily: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    """The n days with the largest daily MAE of the model, and their share of the year's absolute error."""
    top = daily.sort_values('model', ascending=False).head(n).copy()
    top['share_of_abs_error'] = (top.model * top.slots) / (daily.model * daily.slots).sum()
    return top


def profile(frame: pd.DataFrame, by: str) -> pd.DataFrame:
    """MAE of the model and both references by Paris local hour (0 to 23) or month (1 to 12)."""
    kept = common(frame)
    local = kept.index.tz_convert('Europe/Paris')
    key = {'hour': local.hour, 'month': local.month}[by]
    errors = kept[list(FORECASTS)].sub(kept.actual, axis=0).abs()
    return errors.groupby(key).mean().rename_axis(by)


def holidays_in_pool(day: date, window_days: int = 365, lag_days: int = 2) -> int:
    """National holidays among the pool days of the EXP-012 calibration pool of `day`.

    The pool holds the days of the same meteorological season as `day` inside the `window_days`
    days that end `lag_days` before it (`conformal.seasonal_conformal_intervals`).
    """
    last = day - timedelta(days=lag_days)
    season = ev.get_season(day.month)
    first = last - timedelta(days=window_days - 1)
    return sum(ev.get_season(d.month) == season and first <= d <= last
               for year in range(first.year, last.year + 1) for d in ev.holidays(year))


def tail_rank(pool_size: int, level: float = 0.8) -> int | None:
    """Rank of the lower conformal quantile: how many pool residuals must lie beyond an interval end."""
    return order_statistic_ranks(pool_size, level)[0]


def temperature_bins(daily: pd.DataFrame, edges: tuple[float, ...]) -> pd.DataFrame:
    """Days grouped by observed national daily temperature: MAE, bias and coverage per bin."""
    labelled = daily.dropna(subset=['temperature'])
    bins = pd.cut(labelled.temperature, list(edges), right=False)
    grouped = labelled.groupby(bins, observed=False)
    out = grouped[['model', 'rte_star', 'rte_d1', 'bias', 'coverage']].mean()
    out['days'] = grouped.size()
    return out


def boundary_coverage(frame: pd.DataFrame, margin_days: int = 14) -> pd.Series:
    """Interval coverage on days within `margin_days` of a season start, against the other days.

    Winter runs from December to February, so the season starts are 1 March, 1 June, 1 September
    and 1 December. 1 January is not one.
    """
    kept = frame.dropna(subset=['covered'])
    days = pd.Index(ev.local_dates(kept.index))
    starts = [date(y, m, 1) for y in sorted({d.year for d in days} | {min(days).year - 1})
              for m in (3, 6, 9, 12)]
    near = np.array([min(abs((d - s).days) for s in starts) <= margin_days for d in days])
    flags = kept.covered.astype(float).to_numpy()
    return pd.Series({'near_boundary': flags[near].mean(), 'elsewhere': flags[~near].mean(),
                      'near_days': len(set(days[near])), 'other_days': len(set(days[~near]))})


def special_week(day: date, month: int = 12, first: int = 23, last: int = 29) -> bool:
    """True for the Christmas to New Year run of days that the calendar features do not describe."""
    return day.month == month and first <= day.day <= last


def holiday_residuals(frame: pd.DataFrame, days: list[date]) -> pd.DataFrame:
    """Residual (actual minus model) of the given days, one row per day, one column per Paris slot."""
    kept = common(frame)
    local = kept.index.tz_convert('Europe/Paris')
    dates = pd.Index(local.date)
    slot = local.hour * 2 + local.minute // 30
    table = pd.DataFrame({'day': dates, 'slot': slot, 'residual': (kept.actual - kept.model).to_numpy()})
    table = table[table.day.isin(days)]
    return table.pivot(index='day', columns='slot', values='residual')


def leave_one_out_interval(residuals: pd.DataFrame, level: float = 0.8) -> pd.DataFrame:
    """Coverage and width of a per-slot split-conformal interval built from the other days only.

    Same rank rule as the project's conformal intervals (`conformal.window_quantiles`). One row per
    day: share of slots inside the interval and mean width.
    """
    rows = {}
    for day in residuals.index:
        others = residuals.drop(index=day)
        bounds = np.array([window_quantiles(others[slot].to_numpy(), level) for slot in others.columns])
        value = residuals.loc[day].to_numpy()
        rows[day] = {'coverage': float(np.mean((value >= bounds[:, 0]) & (value <= bounds[:, 1]))),
                     'width': float(np.mean(bounds[:, 1] - bounds[:, 0]))}
    return pd.DataFrame(rows).T
