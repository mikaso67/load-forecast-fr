"""Post-hoc diagnostics made after seeing TEST-2025 (not part of the protocol).

1. RTE D-1 corrected with its own 2025 mean error by local hour: an oracle (it uses 2025 actuals), not a forecast.
2. The protocol item 2 count of days won on cold days and holidays, which run_test2025.py did not compute.
Reads data/experiments/TEST-2025/predictions.csv and the 2025 SYNOP labels.
"""

import json
from dataclasses import asdict
from datetime import date

import pandas as pd

from load_forecast import evaluation as ev
from load_forecast.eco2mix import connect_database
from load_forecast.labels import read_labels

frame = pd.read_csv('data/experiments/TEST-2025/predictions.csv', index_col=0, parse_dates=True).dropna()
hour = frame.index.tz_convert('Europe/Paris').hour
bias = (frame.rte_d1 - frame.actual).groupby(hour).mean()
oracle = frame.rte_d1 - pd.Series(hour, index=frame.index).map(bias)
out = {'oracle_mae': float((oracle - frame.actual).abs().mean()),
       'rte_star_mae': float((frame.rte_star - frame.actual).abs().mean())}
for name in ('candidate1', 'candidate2'):
    table = pd.DataFrame({ev.MODEL: frame[name], ev.ACTUAL: frame.actual, ev.RTE: oracle})
    daily = ev.daily_mae(table)
    primary, check = ev.compare_with_check(daily[ev.MODEL], daily[ev.RTE], date(2025, 1, 1), date(2025, 12, 31))
    out[name] = {'mae': float((frame[name] - frame.actual).abs().mean()),
                 'bootstrap_7': asdict(primary) | {'verdict': primary.verdict},
                 'bootstrap_14': asdict(check) | {'verdict': check.verdict}}
with connect_database() as connection:
    labels = read_labels(connection, date(2025, 1, 1), date(2026, 1, 1))
cold = {d: bool(f) for d, f in labels.cold.items() if not pd.isna(f)}
refs = {'rte': frame.rte_d1, 'rte_star': frame.rte_star}
won = {}
for name in ('candidate1', 'candidate2'):
    for ref, series in refs.items():
        table = pd.DataFrame({ev.MODEL: frame[name], ev.ACTUAL: frame.actual, ev.RTE: series})
        daily = ev.daily_mae(table)
        kinds = ev.label_days(daily.index, cold)
        for kind in ('cold', 'holiday'):
            days = daily[kinds.reindex(daily.index).to_numpy() == kind]
            won[f'{name}_vs_{ref}_{kind}'] = f'{int((days[ev.MODEL] < days[ev.RTE]).sum())} of {len(days)}'
out['days_won'] = won
print(json.dumps(out, indent=1))
