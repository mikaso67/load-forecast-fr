"""score the saved EXP-002 predictions with the D9 observed labels. No refit."""

import json
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd
import requests
from run_exp002 import load

from load_forecast import evaluation as ev
from load_forecast.baseline import FIRST, LAST
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.labels import read_labels
from load_forecast.tracking import mlflow_tracking_uri

OUTPUT = Path('data/experiments/EXP-002')
SOURCE_QUERY = """
SELECT source_dataset, count(*) FROM {table}
WHERE value_time >= %(start)s AND value_time < %(end)s
  AND consommation IS NOT NULL AND prevision_j1 IS NOT NULL
GROUP BY source_dataset
"""


def main() -> None:
    mlflow = f'{mlflow_tracking_uri()}/api/2.0/mlflow'
    predictions = pd.read_csv(OUTPUT / 'backtest_predictions.csv', index_col='value_time_utc',
                              parse_dates=True)
    data = load()
    frame = predictions[[ev.MODEL]].copy()
    frame[ev.ACTUAL], frame[ev.RTE] = data.consommation, data.prevision_j1
    with connect_database() as connection:
        labels = read_labels(connection, FIRST, LAST + timedelta(days=1))
        start = pd.Timestamp(FIRST, tz='Europe/Paris').tz_convert('UTC')
        end = pd.Timestamp(LAST + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
        sources = connection.execute(clean_query(SOURCE_QUERY), {'start': start, 'end': end}).fetchall()
    missing_days = [d for d, flag in labels.cold.items() if pd.isna(flag)]
    cold = {d: bool(flag) for d, flag in labels.cold.items() if not pd.isna(flag)}
    kept = ev.common_slots(frame)
    kept_days = set(ev.local_dates(kept.index))
    slot_days = ev.local_dates(frame.index)
    # Main table: a label-missing day counts as not cold (evaluation.py default), reported below.
    main_table = ev.mae_by_day_type(frame, cold)
    # Sensitivity: slots of label-missing days dropped from every row.
    without_missing = ev.mae_by_day_type(frame.loc[~slot_days.isin(missing_days)], cold)
    summary = {
        'n_label_days': len(labels), 'n_cold_days': sum(cold.values()),
        'n_label_missing_days': len(missing_days),
        'label_missing_days': [str(d) for d in missing_days],
        'label_missing_days_with_common_slots': sorted(str(d) for d in set(missing_days) & kept_days),
        'sources': {name: int(n) for name, n in sources},
        'day_types': main_table.to_dict(orient='index'),
        'day_types_without_label_missing_days': without_missing.to_dict(orient='index'),
    }
    print(main_table.round(2).to_string())
    print(without_missing.round(2).to_string())
    print(json.dumps({k: v for k, v in summary.items() if k.startswith(('n_', 'label', 'sources'))},
                     indent=2))
    saved = json.loads((OUTPUT / 'results.json').read_text(encoding='utf-8'))
    run_id = saved['mlflow_run_id']
    stamp = int(time.time() * 1000)
    metrics = {}
    for kind in ('cold', 'cold_any'):
        for key in ('model', 'rte_d1', 'n_days', 'n_slots'):
            metrics[f'{kind}_{key}'] = main_table.loc[kind, key]
    metrics['ordinary_model'] = main_table.loc['ordinary', 'model']
    metrics['ordinary_rte_d1'] = main_table.loc['ordinary', 'rte_d1']
    metrics['n_label_missing_days'] = len(missing_days)
    for block in (7, 14):
        for key, value in saved['vs_exp001'][f'bootstrap_{block}'].items():
            if key != 'verdict':
                metrics[f'vs_exp001_bootstrap_{block}_{key}'] = value
    metrics['vs_exp001_n_slots'] = saved['vs_exp001']['n_slots']
    metrics['vs_exp001_reference_mae'] = saved['vs_exp001']['reference_mae']
    params = {'source_dataset': ','.join(f'{n}:{c}' for n, c in sources),
              'cold_labels': 'D9 SYNOP, 12 stations, 4.84 C, labels.synop'}
    response = requests.post(f'{mlflow}/runs/log-batch', json={
        'run_id': run_id,
        'metrics': [{'key': k, 'value': float(v), 'timestamp': stamp, 'step': 1}
                    for k, v in metrics.items()],
        'params': [{'key': k, 'value': v} for k, v in params.items()]}, timeout=30)
    response.raise_for_status()
    status = requests.get(f'{mlflow}/runs/get', params={'run_id': run_id}, timeout=30)
    status.raise_for_status()
    print('mlflow run', run_id, status.json()['run']['info']['status'])
    (OUTPUT / 'results_labels.json').write_text(json.dumps(saved | summary, indent=2, default=str),
                                                encoding='utf-8')


if __name__ == '__main__':
    main()
