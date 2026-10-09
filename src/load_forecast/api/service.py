"""Cutoff-bounded reads and weekly calendar OLS forecasts, without evaluation."""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from load_forecast import evaluation as ev
from load_forecast.backtest import day_features, forecast_day, known_history, local_slots
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.linear_model import SlotOLS, calendar_ols_features

TRAINING_START = date(2021, 3, 23)
MODEL_NAME = 'EXP-002 calendar OLS'
QUERY = """
SELECT value_time, consommation
FROM {table}
WHERE value_time >= %(start)s AND value_time <= %(last_slot)s
ORDER BY value_time
"""


def refit_date(day: date) -> date:
    """Monday delivery date whose Sunday-noon cutoff initializes this week."""
    return day - timedelta(days=day.weekday())


def read_history(day: date) -> pd.Series:
    """Read targets only, bounded by D2 at the weekly refit, never RTE or labels."""
    start = pd.Timestamp(TRAINING_START, tz='Europe/Paris').tz_convert('UTC')
    query = clean_query(QUERY)
    with connect_database() as connection:
        cursor = connection.execute(query, {
            'start': start, 'last_slot': ev.last_usable_slot_start(refit_date(day)),
        })
        rows = cursor.fetchall()
    index = pd.DatetimeIndex([row[0] for row in rows], tz='UTC')
    return pd.Series([row[1] for row in rows], index=index, dtype=float)


def predict(day: date, history: pd.Series) -> pd.DataFrame:
    """Fit the same 48 calendar models as EXP-002 at its weekly cutoff.

    Historical consolidated revisions remain the accepted protocol limitation.
    This serves forecasts only: no scoring, tuning, test-year evaluation or MLflow writes.
    """
    refit = refit_date(day)
    usable = known_history(history, refit, None).dropna()
    start = pd.Timestamp(TRAINING_START, tz='Europe/Paris').tz_convert('UTC')
    usable = usable.loc[usable.index >= start]
    days = sorted(set(ev.local_dates(usable.index)))
    if not days:
        raise ValueError('No training data available')
    features = pd.concat([
        day_features(usable, training_day, calendar_ols_features) for training_day in days
    ]).reindex(usable.index)
    ids = local_slots(features.index)
    models = {}
    for slot in range(48):
        rows = ids == slot
        if not rows.any():
            raise ValueError('Insufficient training slots')
        models[slot] = SlotOLS().fit(features.loc[rows], usable.loc[rows])
    output = forecast_day(models, usable, day, refit, calendar_ols_features)
    if not np.isfinite(output[ev.MODEL]).all():
        raise ValueError('Non-finite prediction')
    return output
