"""EXP-011 choice on 2023 only (D13, M10 A): the step gamma of the adaptive conformal intervals.

Uses the cached 2023 forecasts of the four experts (scripts/select_aggregation.py), the shared
ML-Poly aggregate of EXP-009 on 2023, the window N = 182 of EXP-010 and the grid fixed in
EXP-011.md. Rule fixed there: the step whose coverage is closest to 80 percent, ties within 0.5
point broken by the smaller mean width. Every step is scored on the slots EXP-010 scored on 2023
(delivery days from 2023-07-03), and gamma = 0 (EXP-010) is printed for context only.
"""

import json
from datetime import date, timedelta

import pandas as pd
from run_exp002 import load
from select_aggregation import CACHE, LAST

from load_forecast import evaluation as ev
from load_forecast.aggregation import aggregate
from load_forecast.conformal import (
    LEVEL,
    adaptive_conformal_intervals,
    coverage_bootstrap,
    first_full_window_day,
    scored_slots,
)

GRID = (0.001, 0.005, 0.01, 0.05)
WINDOW_DAYS = 182
FIRST_RESIDUAL_DAY = date(2023, 1, 1)
TIE_POINTS = 0.005


def main() -> None:
    data = load()
    end = pd.Timestamp(LAST + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
    history = data.consommation.loc[data.index < end]
    experts = pd.read_csv(CACHE, index_col=0, parse_dates=True)
    point = aggregate(experts, history, per_slot=False).forecast
    first_scored = first_full_window_day(FIRST_RESIDUAL_DAY, WINDOW_DAYS)
    days = pd.Series(ev.local_dates(point.index), index=point.index)
    scored_days = days >= first_scored
    rows = {}
    for gamma in (0.0, *GRID):
        bounds = adaptive_conformal_intervals(point, history, window_days=WINDOW_DAYS, gamma=gamma)
        frame = scored_slots(bounds[['lower', 'upper']], point, history)
        frame = frame[scored_days.reindex(frame.index)]
        boot = coverage_bootstrap(frame)
        month = pd.DatetimeIndex(ev.local_dates(frame.index)).month
        rows[gamma] = {'coverage': boot['coverage'], 'low': boot['low'], 'high': boot['high'],
                       'mean_width_mw': float(frame.width.mean()), 'n_slots': boot['n_slots'],
                       'n_days': boot['n_days'],
                       'coverage_by_month': {int(m): round(float(part.covered.mean()), 4)
                                             for m, part in frame.groupby(month)}}
    if len({row['n_slots'] for row in rows.values()}) != 1:
        raise RuntimeError('Steps are scored on different slots')
    gap = {g: abs(rows[g]['coverage'] - LEVEL) for g in GRID}
    best = min(gap.values())
    tied = [g for g in GRID if gap[g] - best <= TIE_POINTS + 1e-12]
    choice = min(tied, key=lambda g: rows[g]['mean_width_mw'])
    print(json.dumps({'inner_period': '2023', 'window_days': WINDOW_DAYS,
                      'first_scored_day': str(first_scored), 'rows': rows,
                      'abs_gap_to_80': gap, 'tied_within_half_point': tied, 'choice': choice},
                     indent=2))


if __name__ == '__main__':
    main()
