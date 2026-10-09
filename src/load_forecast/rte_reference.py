"""D15 RTE* a2: previous Paris calendar year's mean error by local hour.

This preserves docs/rte_benchmark.md section 5.1 exactly. December consolidated
values may be unavailable in early January; this benchmark retains that documented
availability limitation rather than substituting a different correction window.
"""

import numpy as np
import pandas as pd

RTE_STAR = 'rte_star'


def prior_year_hourly_reference(
    actual: pd.Series, rte: pd.Series, target_rte: pd.Series,
) -> pd.Series:
    """Subtract mean (RTE minus actual) in year Y-1 for each target's Paris hour.

    Each Paris hour needs finite actual/RTE pairs on at least 95% of its expected
    half-hour slots in the full prior calendar year, including DST transitions.
    Insufficient coverage raises ValueError, with no filling or alternate year.
    Inputs have unique UTC indices. Only prior-year rows enter each statistic.
    """
    for series in (actual, rte, target_rte):
        if (not isinstance(series.index, pd.DatetimeIndex)
                or str(series.index.tz) != 'UTC' or series.index.has_duplicates):
            raise ValueError('Expected unique UTC timestamps')
    history = pd.DataFrame({'actual': actual, 'rte': rte})
    local = history.index.tz_convert('Europe/Paris')
    target = target_rte.index.tz_convert('Europe/Paris')
    corrected = pd.Series(float('nan'), index=target_rte.index, name=RTE_STAR)
    for year in target.year.unique():
        previous = history.loc[local.year == year - 1]
        grid = pd.date_range(f'{year - 1}-01-01', f'{year}-01-01',
                             freq='30min', inclusive='left', tz='Europe/Paris')
        previous = previous.reindex(grid.tz_convert('UTC'))
        valid = pd.Series(np.isfinite(previous).all(axis=1), index=previous.index)
        coverage = valid.groupby(grid.hour).mean().reindex(range(24), fill_value=0.)
        insufficient = coverage[coverage < 0.95]
        if not insufficient.empty:
            details = ', '.join(f'{hour:02d}: {fraction:.1%}'
                                for hour, fraction in insufficient.items())
            raise ValueError(f'RTE* prior Paris year {year - 1} needs at least 95% '
                             f'finite paired half-hour slots per local hour; {details}')
        previous = previous.loc[valid]
        error = previous.rte - previous.actual
        bias = error.groupby(previous.index.tz_convert('Europe/Paris').hour).mean()
        rows = target.year == year
        adjustment = pd.Series(target.hour[rows], index=target_rte.index[rows]).map(bias)
        corrected.loc[rows] = target_rte.loc[rows] - adjustment
    return corrected
