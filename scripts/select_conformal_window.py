"""EXP-010 choice on 2023 only (D13, M10 A): the calibration window N of the rolling conformal interval.

Uses the cached 2023 forecasts of the four experts (scripts/select_aggregation.py), the shared
ML-Poly aggregate of EXP-009 on 2023, and the grid fixed in EXP-010.md. Rule fixed there: the
window whose coverage is closest to 80 percent, ties within 0.5 point broken by the smaller mean
width. Every window is scored on the same slots: the delivery days whose longest window is full.
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
    conformal_intervals,
    coverage_bootstrap,
    first_full_window_day,
    scored_slots,
)

GRID = (28, 56, 91, 182)
FIRST_RESIDUAL_DAY = date(2023, 1, 1)
TIE_POINTS = 0.005


def main() -> None:
    data = load()
    end = pd.Timestamp(LAST + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
    history = data.consommation.loc[data.index < end]
    experts = pd.read_csv(CACHE, index_col=0, parse_dates=True)
    point = aggregate(experts, history, per_slot=False).forecast
    first_scored = first_full_window_day(FIRST_RESIDUAL_DAY, max(GRID))
    days = pd.Series(ev.local_dates(point.index), index=point.index)
    scored_days = days >= first_scored
    rows = {}
    for window in GRID:
        frame = scored_slots(conformal_intervals(point, history, window_days=window), point, history)
        everything = frame
        frame = frame[scored_days.reindex(frame.index)]
        boot = coverage_bootstrap(frame)
        rows[window] = {'coverage': boot['coverage'], 'low': boot['low'], 'high': boot['high'],
                        'mean_width_mw': float(frame.width.mean()), 'n_slots': boot['n_slots'],
                        'n_days': boot['n_days'],
                        'context_all_defined_days': {
                            'coverage': float(everything.covered.mean()),
                            'mean_width_mw': float(everything.width.mean()),
                            'n_slots': int(len(everything))}}
    if len({row['n_slots'] for row in rows.values()}) != 1:
        raise RuntimeError('Windows are scored on different slots')
    gap = {w: abs(row['coverage'] - LEVEL) for w, row in rows.items()}
    best = min(gap.values())
    tied = [w for w in GRID if gap[w] - best <= TIE_POINTS + 1e-12]
    choice = min(tied, key=lambda w: rows[w]['mean_width_mw'])
    print(json.dumps({'inner_period': '2023', 'first_scored_day': str(first_scored),
                      'rows': rows, 'abs_gap_to_80': gap, 'tied_within_half_point': tied,
                      'choice': choice}, indent=2))


if __name__ == '__main__':
    main()
