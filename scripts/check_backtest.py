"""Reproduce the UTC EXP-001 MAE through the generic backtest runner."""

import json
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from load_forecast import evaluation as ev
from load_forecast.backtest import (
    SeasonalNaive,
    log_backtest,
    naive_features,
    score_backtest,
    walk_forward,
)
from load_forecast.baseline import END, FIRST, LAST, QUERY
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.tracking import mlflow_tracking_uri


def main() -> None:
    with connect_database() as connection:
        cursor = connection.execute(clean_query(QUERY), {
            'start': pd.Timestamp('2021-03-23', tz='Europe/Paris').tz_convert('UTC'), 'end': END})
        data = pd.DataFrame(cursor.fetchall(), columns=[c.name for c in cursor.description])
    data.index = pd.DatetimeIndex(pd.to_datetime(data.pop('value_time'), utc=True))
    predictions = walk_forward(data.consommation, FIRST, LAST, SeasonalNaive,
                               builder=naive_features, training_start=date(2021, 3, 23))
    result = score_backtest(predictions, data.consommation, data.prevision_j1)
    assert abs(result['model']['mae'] - 3249.409986) < 1e-6
    result['mlflow_run_id'] = log_backtest(result, FIRST, LAST, exp='EXP-001',
                                        frame=predictions.assign(actual=data.consommation,
                                                                 rte_d1=data.prevision_j1),
                                        params={'reference': 'RTE D-1',
                                                'bootstrap_seed': ev.BOOTSTRAP_SEED})
    response = requests.get(f'{mlflow_tracking_uri()}/api/2.0/mlflow/runs/get',
                            params={'run_id': result['mlflow_run_id']}, timeout=30)
    response.raise_for_status()
    if response.json()['run']['info']['status'] != 'FINISHED':
        raise RuntimeError('MLflow did not confirm the completed run')
    output = Path('data/experiments/EXP-001')
    output.mkdir(parents=True, exist_ok=True)
    predictions.to_csv(output / 'backtest_predictions.csv', index_label='value_time_utc')
    (output / 'backtest_results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
