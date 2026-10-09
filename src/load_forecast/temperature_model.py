"""Rung 1 plus temperature (EXP-003): heating and cooling degrees with fixed bases (M4 option A).

The builder reads a temperature frame produced by `weather_features.read_temperature`, where every
day already carries the temperature of the run selected by D11 and published before that day's
cutoff. The builder only looks up the slots of the day it is asked for, so it cannot read another
day's weather. Weather-missing slots stay NaN: the runner then leaves them out of the fit (the
rows are not imputed) and `with_calendar_backup` replaces their forecast by the calendar model (M11).
"""

from datetime import date

import pandas as pd

from load_forecast import evaluation as ev
from load_forecast.backtest import FeatureBuilder
from load_forecast.linear_model import calendar_ols_features

HEATING_BASE_C = 15.0
COOLING_BASE_C = 20.0
BACKUP_FLAG = 'backup_calendar'


def degree_features(temperature_c: pd.Series) -> pd.DataFrame:
    """Heating and cooling degrees; NaN temperature gives NaN degrees, never zero."""
    return pd.DataFrame({'hdd': (HEATING_BASE_C - temperature_c).clip(lower=0),
                         'cdd': (temperature_c - COOLING_BASE_C).clip(lower=0)})


def temperature_ols_features(temperature: pd.DataFrame) -> FeatureBuilder:
    """Calendar features of EXP-002 plus hdd and cdd from the selected-run temperature.

    Days before the first covered day (the archive starts one day after the training start) get
    NaN weather and drop out of the fit. A day after the last covered day, or a gap inside the
    covered range, raises: a silent NaN there would hide a coverage hole behind the backup model.
    """
    celsius = temperature['temperature_c']
    if str(celsius.index.tz) != 'UTC' or celsius.index.has_duplicates:
        raise ValueError('Expected a unique UTC temperature index')
    covered = pd.Index(sorted(set(ev.local_dates(celsius.index))))
    first, last = covered.min(), covered.max()

    def builder(slots: pd.DatetimeIndex, history: pd.Series, day: date) -> pd.DataFrame:
        calendar = calendar_ols_features(slots, history, day)
        if day < first:
            values = pd.Series(float('nan'), index=slots)
        elif day > last or day not in covered:
            raise ValueError(f'No temperature recorded for {day}')
        else:
            values = celsius.reindex(slots)
        return calendar.join(degree_features(values))

    return builder


def with_calendar_backup(weather: pd.DataFrame, calendar: pd.DataFrame,
                         flag: str = BACKUP_FLAG) -> pd.DataFrame:
    """M11: where a model has no forecast, use the backup model's and flag the slot.

    The names say EXP-003 (weather model, calendar backup); EXP-004 reuses the function with the
    lagged model, the EXP-003 forecast as backup and its own flag.
    """
    if not weather.index.equals(calendar.index):
        raise ValueError('Weather and calendar forecasts must share the same slots')
    missing = weather[ev.MODEL].isna()
    combined = weather.copy()
    combined.loc[missing, ev.MODEL] = calendar.loc[missing, ev.MODEL]
    combined[flag] = missing
    return combined
