"""Rung 1 plus temperature plus lagged consumption (EXP-004, M3 option A).

Three lags, all allowed by D2: the same local slot of D-2, the same local slot of D-7, and the last
usable value (D-1, 09:30 local slot, the slot that ends two hours before the cutoff). Lags are matched
through UTC timestamps built from the local wall clock (M1), never by row offset, so a 23-hour or
25-hour day cannot shift them. Each lag day also gets a special-day indicator (holiday or bridge),
because a low lag caused by a holiday says nothing about the level of D (M3 caveat).

Missing-lag rule (written once, reused by production): if any lag of a target day is missing, the
whole day carries no lag features. The runner then leaves it out of the fit, and the forecast of that
day is replaced by the previous model and flagged (`with_calendar_backup` with `flag=BACKUP_FLAG`).
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from load_forecast import evaluation as ev
from load_forecast.backtest import FeatureBuilder
from load_forecast.cutoff import PARIS
from load_forecast.linear_model import local_delivery_date
from load_forecast.temperature_model import temperature_ols_features

LAG_COLUMNS = ['lag_d2', 'lag_d7', 'lag_last']
SPECIAL_COLUMNS = ['special_d2', 'special_d7', 'special_last']
BACKUP_FLAG = 'backup_exp003'
LAG_DAYS = {'lag_d2': 2, 'lag_d7': 7}


def same_local_slot_lag(slots: pd.DatetimeIndex, history: pd.Series, days: int) -> pd.Series:
    """Value of the same Paris wall-clock half-hour `days` calendar days earlier, NaN if absent.

    A wall-clock time that does not exist (the 02:xx hour of a spring change) gives NaN, never a
    neighbouring slot. A repeated wall-clock time (autumn change) resolves to the second, standard
    time occurrence: it is the one the source keeps (D12, the first one is missing from the data).
    """
    wall = slots.tz_convert(PARIS).tz_localize(None) - pd.Timedelta(days=days)
    lagged = wall.tz_localize(PARIS, ambiguous=np.zeros(len(wall), dtype=bool),
                              nonexistent='NaT')
    exists = ~lagged.isna()
    values = np.full(len(slots), np.nan)
    values[exists] = history.reindex(lagged[exists].tz_convert('UTC')).to_numpy(dtype=float)
    return pd.Series(values, index=slots)


def is_special_day(day: date) -> bool:
    return ev.is_holiday(day) or ev.is_bridge(day)


def lag_features(slots: pd.DatetimeIndex, history: pd.Series, day: date) -> pd.DataFrame:
    """The three lags and their special-day indicators; all NaN for the day if any lag is missing."""
    if local_delivery_date(slots) != day:
        raise ValueError('Slots must belong to the delivery day')
    columns = {name: same_local_slot_lag(slots, history, k) for name, k in LAG_DAYS.items()}
    last_start = pd.Timestamp(ev.last_usable_slot_start(day))
    columns['lag_last'] = pd.Series(float(history.reindex([last_start]).iloc[0]), index=slots)
    frame = pd.DataFrame(columns)[LAG_COLUMNS].astype(float)
    frame['special_d2'] = float(is_special_day(day - timedelta(days=2)))
    frame['special_d7'] = float(is_special_day(day - timedelta(days=7)))
    frame['special_last'] = float(is_special_day(day - timedelta(days=1)))
    if frame[LAG_COLUMNS].isna().any().any():
        frame[:] = float('nan')
    return frame


def lagged_ols_features(temperature: pd.DataFrame) -> FeatureBuilder:
    """EXP-003 features (calendar, hdd, cdd) plus the lag block."""
    weather = temperature_ols_features(temperature)

    def builder(slots: pd.DatetimeIndex, history: pd.Series, day: date) -> pd.DataFrame:
        return weather(slots, history, day).join(lag_features(slots, history, day))

    return builder
