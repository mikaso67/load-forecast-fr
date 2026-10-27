"""Cutoff-bounded reads, stored forecast serving, and run event readers.

The live EXP-002 model fitting on each request is replaced by reading stored production
forecasts from the serving authority and database run event tables. Legacy calculation
helpers are retained for reference and benchmark tests.
"""

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from load_forecast import evaluation as ev
from load_forecast.backtest import day_features, forecast_day, known_history, local_slots
from load_forecast.cutoff import PARIS
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.linear_model import SlotOLS, calendar_ols_features
from load_forecast.serving import ServingAuthority

TRAINING_START = date(2021, 3, 23)
MODEL_NAME = 'EXP-002 calendar OLS'
QUERY = """
SELECT value_time, consommation
FROM {table}
WHERE value_time >= %(start)s AND value_time <= %(last_slot)s
ORDER BY value_time
"""


def get_serving_authority() -> ServingAuthority:
    """Return the configured serving authority directory handler."""
    root = Path(os.environ.get('LFF_SERVING_DIR', 'serving'))
    return ServingAuthority(root)


def _parse_time(value: str | datetime) -> datetime:
    if isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        dt = value
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def read_served_forecast(day: date, authority: ServingAuthority | None = None) -> dict[str, Any]:
    """Read the final served forecast from the serving authority choice file.

    Never computes a new prediction; reads the immutable published choice and values.
    """
    if authority is None:
        authority = get_serving_authority()
    choice = authority.read_choice(day)
    kind = choice['kind']
    chosen_at = _parse_time(choice['chosen_at'])
    choice_hash = choice['choice_hash']

    if kind == 'none':
        return {
            'delivery_date': day,
            'kind': 'none',
            'model_or_fallback': None,
            'choice_hash': choice_hash,
            'chosen_at': chosen_at,
            'interval': None,
            'values': [],
        }

    archive = authority.read_served(day)
    if archive is None:
        raise ValueError('Choice specifies values but archive could not be read')

    if kind == 'primary':
        model_name = archive.get('result', {}).get('model_name')
        if not model_name:
            model_name = 'EXP-009 ML-Poly aggregate'
    else:
        models = archive.get('models', [])
        model_name = ', '.join(models) if isinstance(models, list) else str(models or 'fallback')

    has_interval = any(
        row.get('lower') is not None and row.get('upper') is not None
        for row in archive.get('values', [])
    )
    interval_info = (
        {'level': 0.8, 'type': 'seasonal_split_conformal'}
        if has_interval else None
    )

    formatted_values = []
    for row in archive.get('values', []):
        utc_stamp = _parse_time(row['value_time'])
        paris_stamp = utc_stamp.astimezone(PARIS)
        lower_val = float(row['lower']) if row.get('lower') is not None else None
        upper_val = float(row['upper']) if row.get('upper') is not None else None
        n_pool_val = int(row['n_pool']) if row.get('n_pool') is not None else None
        formatted_values.append({
            'timestamp_utc': utc_stamp,
            'timestamp_paris': paris_stamp,
            'local_slot': int(row['local_slot']),
            'forecast_mw': float(row['point']),
            'lower_mw': lower_val,
            'upper_mw': upper_val,
            'n_pool': n_pool_val,
        })

    return {
        'delivery_date': day,
        'kind': kind,
        'model_or_fallback': model_name,
        'choice_hash': choice_hash,
        'chosen_at': chosen_at,
        'interval': interval_info,
        'values': formatted_values,
    }


def read_run_events(day: date, connection: Any = None) -> list[dict[str, Any]]:
    """Query run start and completion events from the forecast schema for delivery day."""
    query = """
    SELECT s.run_id, s.delivery_day, s.freeze_time, s.kind, s.started_at, s.code_commit,
           r.completed_at, r.status, r.refit_day, r.mlflow_run_id, r.kalman_quarter,
           r.weather_status, r.last_consumption_slot, r.max_ingested_at, r.feature_hash, r.flags
    FROM forecast.run_starts s
    LEFT JOIN forecast.run_results r ON s.run_id = r.run_id
    WHERE s.delivery_day = %(day)s
    ORDER BY s.started_at ASC
    """
    if connection is not None:
        return _fetch_run_rows(connection, query, day)
    with connect_database() as conn:
        return _fetch_run_rows(conn, query, day)


def _fetch_run_rows(connection: Any, query: str, day: date) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(query, {'day': day})
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
    events = []
    for row in rows:
        record = dict(zip(columns, row, strict=True))
        if record.get('flags') and isinstance(record['flags'], str):
            try:
                record['flags'] = json.loads(record['flags'])
            except (json.JSONDecodeError, TypeError):
                pass
        events.append(record)
    return events


def refit_date(day: date) -> date:
    """Monday delivery date whose Sunday-noon cutoff initializes this week."""
    return day - timedelta(days=day.weekday())


def read_history(day: date) -> pd.Series:
    """Legacy helper: Read targets only, bounded by D2 at the weekly refit."""
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
    """Legacy helper: Fit the same 48 calendar models as EXP-002 at its weekly cutoff."""
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

