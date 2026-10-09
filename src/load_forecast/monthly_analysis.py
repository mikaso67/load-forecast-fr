"""Monthly diagnostics of frozen forecasts on common UTC timestamps."""

import numpy as np
import pandas as pd

FORECASTS = ('exp005', 'exp006', 'exp007')


def monthly_comparison(frame: pd.DataFrame) -> pd.DataFrame:
    """Return slot-weighted MAE, signed bias and gains by Paris calendar month.

    Positive gain means lower MAE than EXP-005. All models and raw RTE use
    the same finite observations; missing values are excluded, never filled.
    """
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError('Expected timezone-aware timestamps')
    if frame.index.has_duplicates:
        raise ValueError('Duplicate timestamps')
    columns = ['actual', 'rte_d1', *FORECASTS]
    kept = frame[columns].replace([np.inf, -np.inf], np.nan).dropna()
    if kept.empty:
        raise ValueError('No common slots')
    months = kept.index.tz_convert('Europe/Paris').strftime('%Y-%m')
    errors = kept[['rte_d1', *FORECASTS]].sub(kept.actual, axis=0)
    result = pd.DataFrame({'slots': kept.groupby(months).size()})
    for name in errors:
        result[f'{name}_mae'] = errors[name].abs().groupby(months).mean()
        result[f'{name}_bias'] = errors[name].groupby(months).mean()
    for name in FORECASTS[1:]:
        result[f'{name}_gain'] = result.exp005_mae - result[f'{name}_mae']
    result.index.name = 'month'
    return result
