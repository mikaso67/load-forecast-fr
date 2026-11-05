"""Unserved morning level correction, stored separately from immutable serving archives."""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from load_forecast import monitoring
from load_forecast.aggregation import known_morning
from load_forecast.cutoff import delivery_slots, forecast_cutoff

SHRINKAGE = 0.3


def build_shadow(archive: dict, previous: dict | None, history: pd.Series,
                 mapped: pd.Series, realtime: pd.Series, snapshot_hash: str) -> dict:
    """Use every expected D-1 morning slot, or apply zero with an explicit reason.

    Inputs are the already frozen production snapshot. The previous points are the original
    aggregate, never yesterday's corrected shadow, matching the validated level correction.
    """
    day = date.fromisoformat(archive['delivery_day'])
    variant = archive['variant']
    if variant not in ('A', 'B'):
        raise ValueError('Expected A/B variant')
    yesterday = day - timedelta(days=1)
    grid = pd.DatetimeIndex(delivery_slots(yesterday, '30min'))
    morning = grid[known_morning(grid, day)]
    actual = history.reindex(morning)
    adjusted = mapped.reindex(morning) if variant == 'B' else actual.copy()
    reason = None
    if previous is None:
        reason = 'missing_previous_forecast'
    elif (previous['delivery_day'] != str(yesterday) or previous['variant'] != variant
          or previous['kind'] != 'primary'
          or pd.Timestamp(previous['start']['freeze_time']) != forecast_cutoff(yesterday)
          or pd.Timestamp(previous['result']['completed_at']) >= forecast_cutoff(day)):
        reason = 'unavailable_previous_forecast'
    prediction = pd.Series(dtype=float, index=pd.DatetimeIndex([], tz='UTC'))
    if reason is None:
        rows = previous['values']
        prediction = pd.Series([r['point'] for r in rows],
            index=pd.to_datetime([r['value_time'] for r in rows], utc=True)).reindex(morning)
        if not np.isfinite(prediction).all():
            reason = 'missing_previous_morning_forecast'
        elif (not np.isfinite(actual).all() or not np.isfinite(adjusted).all()
              or not realtime.reindex(morning, fill_value=False).all()):
            reason = 'missing_morning_actuals'
    error = float((prediction - adjusted).mean()) if reason is None else None
    shift = -SHRINKAGE * error if error is not None else 0.0
    gap = float((adjusted - actual).mean()) if np.isfinite(adjusted - actual).all() else None

    def finite(value):
        return float(value) if pd.notna(value) and np.isfinite(value) else None

    return {'run_id': archive['id'], 'delivery_day': str(day), 'variant': variant,
            'snapshot_hash': snapshot_hash, 'previous_run_id': previous['id'] if previous else None,
            'shrinkage': SHRINKAGE, 'morning_error_mw': error, 'shift_mw': shift,
            'mapped_minus_realtime_mw': gap, 'reason': reason,
            'clean_delivery': not bool(archive['result']['flags'].get('degraded_inputs')),
            'morning': [{'value_time': stamp.isoformat(), 'realtime_mw': finite(actual.loc[stamp]),
                         'mapped_mw': finite(adjusted.loc[stamp]),
                         'forecast_mw': finite(prediction.get(stamp, np.nan))} for stamp in morning],
            'values': [{'value_time': row['value_time'], 'point': row['point'] + shift,
                        'lower': None, 'upper': None} for row in archive['values']]}


def shadow_metrics(day: date, actuals: pd.DataFrame, vintage: str, shadow: dict,
                   baseline: pd.DataFrame, served: pd.DataFrame | None) -> dict:
    """Score a complete shadow day without changing the existing scoring grid or metrics."""
    grid = pd.DatetimeIndex(delivery_slots(day, '30min'))
    frame = pd.DataFrame(shadow['values'])
    frame.index = pd.to_datetime(frame.pop('value_time'), utc=True)
    frame = frame.reindex(grid).astype(float)
    if not np.isfinite(frame.point).all():
        raise ValueError('Incomplete shadow forecast')
    target = actuals.reindex(grid)
    if (target.ingested_at.isna().any()
            or not np.isfinite(target.consommation.astype(float)).all()):
        raise ValueError('Incomplete shadow actuals')
    scores = monitoring.score_day(target.consommation.astype(float),
        target.prevision_j1.astype(float), baseline, frame, None, vintage)
    headline = monitoring.score_day(target.consommation.astype(float),
        target.prevision_j1.astype(float), served, frame, None, vintage)
    return {'run_id': shadow['run_id'], 'reason': shadow['reason'],
            'clean_delivery': shadow['clean_delivery'], 'shift_mw': shadow['shift_mw'],
            'shadow': scores['model'], 'baseline': scores['served'],
            'served': headline['served'], 'rte_d1': scores['rte_d1']}
