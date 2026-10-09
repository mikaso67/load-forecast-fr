"""Compare the two seasonal lags on identical 2024 slots and log local MLflow."""

import json
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import requests

from load_forecast import evaluation as ev
from load_forecast.baseline import END, FIRST, HISTORY_START, LAST, QUERY, START, seasonal_naive
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.tracking import backtest_tags, mlflow_tracking_uri


def main() -> None:
    with connect_database() as connection:
        cursor = connection.execute(clean_query(QUERY), {'start': HISTORY_START, 'end': END})
        data = pd.DataFrame(cursor.fetchall(), columns=[c.name for c in cursor.description])
    data.index = pd.DatetimeIndex(pd.to_datetime(data.pop('value_time'), utc=True))
    frame = pd.DataFrame(index=pd.date_range(START, END, freq='30min', inclusive='left'))
    frame[ev.ACTUAL] = data['consommation']
    frame[ev.RTE] = data['prevision_j1']
    for basis in ('utc', 'local'):
        frame[basis] = pd.concat([seasonal_naive(data.consommation, day, time_basis=basis)
                                 for day in pd.date_range(FIRST, LAST).date])
    common = frame.dropna()
    result = {'n_expected': len(frame), 'n_common': len(common),
              'excluded': [t.isoformat() for t in frame.index.difference(common.index)]}
    daily = {}
    for basis in ('utc', 'local'):
        scored = common.rename(columns={basis: ev.MODEL})
        scores = ev.score(scored)
        daily[basis] = ev.daily_mae(scored)[ev.MODEL]
        result[basis] = {'model': asdict(scores.model), 'rte': asdict(scores.rte),
                         'day_types': ev.mae_by_day_type(scored).to_dict(orient='index')}
        rte_daily = ev.daily_mae(scored)[ev.RTE]
        primary, sensitivity = ev.compare_with_check(daily[basis], rte_daily, FIRST, LAST)
        result[basis]['difference_vs_rte_7'] = asdict(primary)
        result[basis]['difference_vs_rte_14'] = asdict(sensitivity)
    primary, sensitivity = ev.compare_with_check(daily['local'], daily['utc'], FIRST, LAST)
    result['difference_local_minus_utc'] = asdict(primary)
    result['sensitivity_14'] = asdict(sensitivity)
    timestamp = int(time.time()*1000)
    url = f'{mlflow_tracking_uri()}/api/2.0/mlflow'
    with requests.Session() as session:
        response = session.post(f'{url}/runs/create', json={
            'experiment_id': '0', 'start_time': timestamp,
            'tags': [{'key': k, 'value': v} for k, v in (backtest_tags(FIRST, LAST, 'EXP-001') |
                {'mlflow.runName': 'EXP-001 local lag addendum'}).items()]}, timeout=30)
        response.raise_for_status()
        run_id = response.json()['run']['info']['run_id']
        metrics = {f'{basis}_{who}_{name}': value for basis in ('utc', 'local')
                   for who in ('model', 'rte') for name, value in result[basis][who].items()}
        for basis in ('utc', 'local'):
            for kind, row in result[basis]['day_types'].items():
                if kind not in ('cold', 'cold_any'):
                    metrics.update({f'{basis}_{kind}_{key}': value for key, value in row.items()})
            for block in (7, 14):
                metrics.update({f'{basis}_vs_rte_{block}_{key}': value for key, value in
                                result[basis][f'difference_vs_rte_{block}'].items()})
        for name in ('difference_local_minus_utc', 'sensitivity_14'):
            metrics.update({f'{name}_{key}': value for key, value in result[name].items()})
        response = session.post(f'{url}/runs/log-batch', json={
            'run_id': run_id, 'metrics': [{'key': k, 'value': float(v),
                                         'timestamp': timestamp, 'step': 0}
                                        for k, v in metrics.items()],
            'params': [{'key': 'dst_lag_policy', 'value': 'NaN for ambiguous or nonexistent'},
                       {'key': 'n_common', 'value': str(len(common))}]}, timeout=30)
        response.raise_for_status()
        response = session.post(f'{url}/runs/update', json={
            'run_id': run_id, 'status': 'FINISHED', 'end_time': int(time.time()*1000)}, timeout=30)
        response.raise_for_status()
        response = session.get(f'{url}/runs/get', params={'run_id': run_id}, timeout=30)
        response.raise_for_status()
        if response.json()['run']['info']['status'] != 'FINISHED':
            raise RuntimeError('MLflow did not confirm the completed run')
    result['mlflow_run_id'] = run_id
    output = Path('data/experiments/EXP-001')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'time_basis_comparison.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
