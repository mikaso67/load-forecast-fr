"""Fixed EXP-001 orchestration. All scoring logic belongs to evaluation.py."""

from dataclasses import asdict
from datetime import date

import pandas as pd

from load_forecast import evaluation as ev
from load_forecast.cutoff import available_at, delivery_slots, forecast_cutoff

FIRST = date(2024, 1, 1)
LAST = date(2024, 12, 31)
START = pd.Timestamp('2024-01-01', tz='Europe/Paris').tz_convert('UTC')
END = pd.Timestamp('2025-01-01', tz='Europe/Paris').tz_convert('UTC')
HISTORY_START = START - pd.Timedelta(days=7)
QUERY = """
SELECT value_time, consommation, prevision_j1, source_dataset
FROM {table}
WHERE value_time >= %(start)s AND value_time < %(end)s
ORDER BY value_time
"""


def seasonal_naive(
    history: pd.Series, target_day: date, *, time_basis: str = 'utc',
    published_at: pd.Series | None = None,
) -> pd.Series:
    """Seven-day UTC or Paris wall-clock lag, with ambiguous source hours left missing.

    D2 and optional publication times both apply. Consolidated historical values
    retain the accepted revision limitation documented in evaluation.seasonal_naive.
    """
    if time_basis == 'utc':
        return ev.seasonal_naive(history, target_day, published_at=published_at)
    if time_basis != 'local':
        raise ValueError('time_basis must be utc or local')
    if not isinstance(history.index, pd.DatetimeIndex) or str(history.index.tz) != 'UTC':
        raise ValueError('Expected UTC history')
    if history.index.has_duplicates:
        raise ValueError('Duplicate history timestamps')
    usable = history.loc[history.index <= ev.last_usable_slot_start(target_day)]
    if published_at is not None:
        published = published_at.reindex(usable.index)
        if not isinstance(published.dtype, pd.DatetimeTZDtype):
            raise ValueError('Publication timestamps must be aware')
        usable = available_at(pd.DataFrame({'value': usable, 'published_at': published}),
                              forecast_cutoff(target_day))['value']
    slots = pd.DatetimeIndex(delivery_slots(target_day, '30min'))
    labels = slots.tz_convert('Europe/Paris').tz_localize(None) - pd.Timedelta(days=7)
    source = labels.tz_localize('Europe/Paris', ambiguous='NaT', nonexistent='NaT').tz_convert('UTC')
    return pd.Series(usable.reindex(source).to_numpy(dtype=float), index=slots, name='seasonal_naive')


def evaluate_baseline(data: pd.DataFrame, *, time_basis: str = 'utc') -> tuple[dict, pd.DataFrame]:
    """Evaluate only the 2024 Paris calendar, retaining absent slots and days."""
    if not isinstance(data.index, pd.DatetimeIndex) or str(data.index.tz) != 'UTC':
        raise ValueError('Expected an aware UTC index')
    if data.index.has_duplicates:
        raise ValueError('Duplicate source slots')
    if ((data.index < HISTORY_START) | (data.index >= END)).any():
        raise ValueError('Input must contain only the seven-day warmup and validation year')
    slots = pd.date_range(START, END, freq='30min', inclusive='left')
    frame = pd.DataFrame(index=slots)
    frame[ev.ACTUAL] = data['consommation']
    frame[ev.RTE] = data['prevision_j1']
    days = pd.date_range(FIRST, LAST, freq='D').date
    frame[ev.MODEL] = pd.concat([seasonal_naive(data['consommation'], d, time_basis=time_basis)
                               for d in days])
    scores = ev.score(frame)
    daily = ev.daily_mae(frame)
    primary, check = ev.compare_with_check(daily[ev.MODEL], daily[ev.RTE], FIRST, LAST)
    types = ev.mae_by_day_type(frame).drop(index=['cold', 'cold_any'])
    excluded = pd.DataFrame(index=scores.excluded)
    for column in (ev.ACTUAL, ev.MODEL, ev.RTE):
        excluded[f'{column}_missing'] = frame.loc[scores.excluded, column].isna()
    sources = data['source_dataset'].reindex(ev.common_slots(frame).index).value_counts()
    result = {
        'model': asdict(scores.model), 'rte': asdict(scores.rte),
        'n_expected': len(slots), 'n_excluded': scores.n_excluded,
        'excluded_by_date': excluded.groupby(ev.local_dates(excluded.index)).size().to_dict(),
        'day_types': types.to_dict(orient='index'),
        'cold': 'not available until D9 is fixed; ordinary includes unlabelled cold days',
        'bootstrap_7': {**asdict(primary), 'verdict': primary.verdict},
        'bootstrap_14': {**asdict(check), 'verdict': check.verdict},
        'actual_sources': sources.to_dict(),
    }
    result['excluded_by_date'] = {str(k): int(v) for k, v in result['excluded_by_date'].items()}
    return result, excluded
