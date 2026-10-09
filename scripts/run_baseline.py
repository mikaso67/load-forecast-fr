"""Run EXP-001 on the bounded clean table and log it to local MLflow."""

import json
import os
from pathlib import Path

import pandas as pd
import psycopg
import requests

from load_forecast import evaluation as ev
from load_forecast.backtest import log_backtest, score_backtest
from load_forecast.baseline import END, FIRST, HISTORY_START, LAST, QUERY, START, evaluate_baseline
from load_forecast.database import clean_query
from load_forecast.rte_reference import RTE_STAR, prior_year_hourly_reference
from load_forecast.tracking import mlflow_tracking_uri


def main() -> None:
    with psycopg.connect(
        host=os.environ.get('POSTGRES_HOST', '127.0.0.1'),
        port=os.environ.get('POSTGRES_PORT', '5432'),
        dbname=os.environ.get('POSTGRES_DB', 'load_forecast'),
        user=os.environ['POSTGRES_USER'], password=os.environ['POSTGRES_PASSWORD'],
    ) as connection:
        with connection.cursor() as cursor:
            reference_start = pd.Timestamp('2023-01-01', tz='Europe/Paris').tz_convert('UTC')
            cursor.execute(clean_query(QUERY), {'start': reference_start.to_pydatetime(),
                                   'end': END.to_pydatetime()})
            data = pd.DataFrame(cursor.fetchall(), columns=[c.name for c in cursor.description])
    data['value_time'] = pd.to_datetime(data['value_time'], utc=True)
    data = data.set_index('value_time')
    result, excluded = evaluate_baseline(data.loc[data.index >= HISTORY_START])
    frame = pd.DataFrame(index=pd.date_range(START, END, freq='30min', inclusive='left'))
    frame[ev.ACTUAL], frame[ev.RTE] = data.consommation, data.prevision_j1
    frame[ev.MODEL] = pd.concat([ev.seasonal_naive(data.consommation, day)
                                for day in pd.date_range(FIRST, LAST).date])
    frame[RTE_STAR] = prior_year_hourly_reference(data.consommation, data.prevision_j1,
                                                 frame[ev.RTE])
    result.update(score_backtest(frame, frame[ev.ACTUAL], frame[ev.RTE], frame[RTE_STAR]))
    result['mlflow_run_id'] = log_backtest(
        result, FIRST, LAST, exp='EXP-001', frame=frame, params={
            'lag_days': 7, 'consumption_delay_hours': 2, 'bootstrap_seed': ev.BOOTSTRAP_SEED,
            'bootstrap_draws': ev.BOOTSTRAP_DRAWS, 'reference': 'RTE D-1 (first experiment)',
            'cold': 'unavailable', 'actual_sources': result['actual_sources'],
            'history_start_utc': HISTORY_START.isoformat(), 'end_exclusive_utc': END.isoformat(),
            'rte_star': 'a2: prior full Paris calendar year mean error by local hour',
            'rte_star_availability': 'January December consolidation caveat, docs/rte_benchmark.md 5.1',
        })
    response = requests.get(f'{mlflow_tracking_uri()}/api/2.0/mlflow/runs/get',
                            params={'run_id': result['mlflow_run_id']}, timeout=30)
    response.raise_for_status()
    if response.json()['run']['info']['status'] != 'FINISHED':
        raise RuntimeError('MLflow did not confirm the completed run')
    output = Path('data/experiments/EXP-001')
    output.mkdir(parents=True, exist_ok=True)
    excluded.to_csv(output / 'excluded_slots.csv', index_label='value_time_utc')
    (output / 'results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
