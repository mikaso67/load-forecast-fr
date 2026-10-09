"""EXP-009 choice on 2023 only (D13): ML-Poly weights per slot, or one shared weight vector.

Computes the four frozen experts on 2023 (weekly refits from 2021-03-23, consumption deleted after
2023-12-31, weather read to 2023-12-31) and caches them outside git for the burn-in of the 2024 run.
Rule fixed in EXP-009.md: lowest 2023 MAE of the aggregate, `shared` if within 0.5 MW.
"""

import json
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from expert_forecasts import EXPERTS, expert_forecasts
from run_exp002 import load

from load_forecast import evaluation as ev
from load_forecast.aggregation import aggregate

FIRST, LAST = date(2023, 1, 1), date(2023, 12, 31)
CACHE = Path('data/experiments/EXP-009/experts_2023.csv')
TIE_MW = 0.5


def mae(forecast: pd.Series, actual: pd.Series) -> dict:
    kept = pd.DataFrame({'f': forecast, 'y': actual}).dropna()
    return {'mae_2023': float((kept.f - kept.y).abs().mean()), 'n_slots': int(len(kept))}


def main() -> None:
    data = load()
    end = pd.Timestamp(LAST + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
    history = data.consommation.loc[data.index < end]
    began = time.perf_counter()
    if CACHE.exists():
        experts = pd.read_csv(CACHE, index_col=0, parse_dates=True)
        backups = {}
    else:
        experts, backups = expert_forecasts(history, FIRST, LAST)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        experts.to_csv(CACHE, index_label='value_time_utc')
        experts = pd.read_csv(CACHE, index_col=0, parse_dates=True)
    seconds = round(time.perf_counter() - began, 1)
    rows = {name: mae(experts[name], history) for name in EXPERTS}
    rows['uniform_mean'] = mae(experts.mean(axis=1), history)
    for label, per_slot in (('per_slot', True), ('shared', False)):
        rows[label] = mae(aggregate(experts, history, per_slot=per_slot).forecast, history)
    if len({row['n_slots'] for row in rows.values()}) != 1:
        raise RuntimeError('Candidates score different slots')
    gap = rows['per_slot']['mae_2023'] - rows['shared']['mae_2023']
    choice = 'per_slot' if gap < -TIE_MW else 'shared'
    print(json.dumps({'inner_period': '2023', 'rows': rows, 'per_slot_minus_shared_mw': gap,
                      'choice': choice, 'seconds': seconds, 'backup_slots': backups,
                      'n_days': int(len(set(ev.local_dates(experts.index))))}, indent=2))


if __name__ == '__main__':
    main()
