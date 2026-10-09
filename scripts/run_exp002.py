"""EXP-002: one OLS per local slot on calendar features, validation year 2024 only."""

import json
from dataclasses import asdict
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
from load_forecast.baseline import END, FIRST, LAST
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.linear_model import SlotOLS, calendar_ols_features
from load_forecast.rte_reference import RTE_STAR, prior_year_hourly_reference
from load_forecast.tracking import mlflow_tracking_uri

TRAINING_START = date(2021, 3, 23)
QUERY = """
SELECT value_time, consommation, prevision_j1
FROM {table}
WHERE value_time >= %(start)s AND value_time < %(end)s
ORDER BY value_time
"""


def load() -> pd.DataFrame:
    start = pd.Timestamp(TRAINING_START, tz='Europe/Paris').tz_convert('UTC')
    with connect_database() as connection:
        cursor = connection.execute(clean_query(QUERY), {'start': start, 'end': END})
        data = pd.DataFrame(cursor.fetchall(), columns=[c.name for c in cursor.description])
    data.index = pd.DatetimeIndex(pd.to_datetime(data.pop('value_time'), utc=True))
    return data


def paired_against_reference(model: pd.Series, reference: pd.Series, data: pd.DataFrame) -> dict:
    """Daily MAE differences on the slots where model, reference, actual and RTE all exist."""
    frame = pd.DataFrame({ev.MODEL: model, ev.ACTUAL: data.consommation, ev.RTE: data.prevision_j1})
    frame.loc[reference.reindex(frame.index).isna(), ev.MODEL] = float('nan')
    ref_frame = frame.assign(**{ev.MODEL: reference.reindex(frame.index)})
    ref_frame.loc[frame[ev.MODEL].isna(), ev.MODEL] = float('nan')
    daily_model, daily_ref = ev.daily_mae(frame), ev.daily_mae(ref_frame)
    p7, p14 = ev.compare_with_check(daily_model[ev.MODEL], daily_ref[ev.MODEL], FIRST, LAST)
    return {'n_slots': int(ev.common_slots(frame).shape[0]),
            'model_mae': ev.score(frame).model.mae, 'reference_mae': ev.score(ref_frame).model.mae,
            'bootstrap_7': asdict(p7) | {'verdict': p7.verdict},
            'bootstrap_14': asdict(p14) | {'verdict': p14.verdict}}


def main() -> None:
    data = load()
    predictions = walk_forward(data.consommation, FIRST, LAST, SlotOLS,
                               builder=calendar_ols_features, training_start=TRAINING_START)
    frame = predictions.assign(actual=data.consommation, rte_d1=data.prevision_j1)
    frame[RTE_STAR] = prior_year_hourly_reference(data.consommation, data.prevision_j1,
                                                 frame[ev.RTE])
    result = score_backtest(predictions, data.consommation, data.prevision_j1, frame[RTE_STAR])
    naive = walk_forward(data.consommation, FIRST, LAST, SeasonalNaive,
                         builder=naive_features, training_start=TRAINING_START)[ev.MODEL]
    result['vs_exp001'] = paired_against_reference(predictions[ev.MODEL], naive, data)
    result['mlflow_run_id'] = log_backtest(
        result, FIRST, LAST, exp='EXP-002', frame=frame,
        reference=naive, reference_name='EXP-001', params={
            'model': 'OLS per local half-hour slot (48 models), calendar features only',
            'features': 'dow (holiday as Sunday), Fourier order 3, bridge, christmas_week',
            'training_start': TRAINING_START, 'refit': 'weekly, Monday cutoff, expanding window',
            'seed': 'none (deterministic least squares)', 'bootstrap_seed': ev.BOOTSTRAP_SEED,
            'reference': 'EXP-001',
            'rte_star': 'a2: prior full Paris calendar year mean error by local hour',
            'rte_star_availability': 'January December consolidation caveat, docs/rte_benchmark.md 5.1'})
    response = requests.get(f'{mlflow_tracking_uri()}/api/2.0/mlflow/runs/get',
                            params={'run_id': result['mlflow_run_id']}, timeout=30)
    response.raise_for_status()
    if response.json()['run']['info']['status'] != 'FINISHED':
        raise RuntimeError('MLflow did not confirm the completed run')
    output = Path('data/experiments/EXP-002')
    output.mkdir(parents=True, exist_ok=True)
    predictions.to_csv(output / 'backtest_predictions.csv', index_label='value_time_utc')
    (output / 'results.json').write_text(json.dumps(result, indent=2, default=str),
                                         encoding='utf-8')
    print(json.dumps(result, indent=2, default=str))


if __name__ == '__main__':
    main()
