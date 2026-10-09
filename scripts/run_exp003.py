"""EXP-003: EXP-002 plus heating and cooling degrees (fixed bases), validation year 2024 only."""

import json
import sys
import time
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
from run_exp002 import TRAINING_START, load, paired_against_reference

from load_forecast import evaluation as ev
from load_forecast.backtest import log_backtest, score_backtest, walk_forward
from load_forecast.baseline import FIRST, LAST
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.labels import read_labels
from load_forecast.linear_model import SlotOLS, calendar_ols_features
from load_forecast.rte_reference import RTE_STAR, prior_year_hourly_reference
from load_forecast.temperature_model import (
    BACKUP_FLAG,
    COOLING_BASE_C,
    HEATING_BASE_C,
    temperature_ols_features,
    with_calendar_backup,
)
from load_forecast.tracking import mlflow_tracking_uri
from load_forecast.weather_features import read_temperature

WEATHER_FIRST = date(2021, 3, 24)
OUTPUT = Path('data/experiments/EXP-003')
EXP002_MAE = 2871.18
SOURCE_QUERY = """
SELECT source_dataset, count(*) FROM {table}
WHERE value_time >= %(start)s AND value_time < %(end)s
  AND consommation IS NOT NULL AND prevision_j1 IS NOT NULL
GROUP BY source_dataset
"""


def day_type_table(model: pd.Series, actual: pd.Series, reference: pd.Series,
                   cold: dict[date, bool]) -> pd.DataFrame:
    """Slot-weighted MAE by day type on the slots where model, actual and reference all exist."""
    frame = pd.DataFrame({ev.MODEL: model, ev.ACTUAL: actual, ev.RTE: reference})
    return ev.mae_by_day_type(frame, cold)


def monthly_diagnostic(frame: pd.DataFrame) -> pd.DataFrame:
    kept = ev.common_slots(frame)
    month = kept.index.tz_convert('Europe/Paris').month
    absolute = kept[[c for c in kept.columns if c != ev.ACTUAL]].sub(kept[ev.ACTUAL], axis=0)
    return pd.concat({'mae': absolute.abs().groupby(month).mean(),
                      'bias': absolute.groupby(month).mean()}, axis=1).round(1)


def main() -> None:
    data = load()
    with connect_database() as connection:
        temperature = read_temperature(connection, WEATHER_FIRST, LAST)
        labels = read_labels(connection, FIRST, LAST + timedelta(days=1))
        start = pd.Timestamp(FIRST, tz='Europe/Paris').tz_convert('UTC')
        end = pd.Timestamp(LAST + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
        sources = connection.execute(clean_query(SOURCE_QUERY), {'start': start, 'end': end}).fetchall()
    days = pd.Series(ev.local_dates(temperature.index), index=temperature.index)
    status = temperature['weather_status'].groupby(days).first().value_counts().to_dict()
    if status.get('not_collected'):
        raise RuntimeError(f'Days without collected TMP rows: {status}')
    missing_slots = temperature['temperature_c'].isna()
    missing_days = sorted(set(days[missing_slots]))
    print('weather status by day', status, '| slots without temperature', int(missing_slots.sum()),
          '| days affected', [str(d) for d in missing_days])

    calendar = walk_forward(data.consommation, FIRST, LAST, SlotOLS,
                            builder=calendar_ols_features, training_start=TRAINING_START)
    weather = walk_forward(data.consommation, FIRST, LAST, SlotOLS,
                           builder=temperature_ols_features(temperature),
                           training_start=TRAINING_START)
    predictions = with_calendar_backup(weather, calendar)
    flagged = predictions[BACKUP_FLAG]
    flagged_days = sorted(set(ev.local_dates(predictions.index[flagged])))
    print('slots on the calendar backup', int(flagged.sum()), '| days', flagged_days)

    frame = predictions.assign(actual=data.consommation, rte_d1=data.prevision_j1)
    frame[RTE_STAR] = prior_year_hourly_reference(data.consommation, data.prevision_j1,
                                                 frame[ev.RTE])
    result = score_backtest(predictions, data.consommation, data.prevision_j1, frame[RTE_STAR])
    exp002 = score_backtest(calendar, data.consommation, data.prevision_j1, frame[RTE_STAR])
    if abs(exp002['model']['mae'] - EXP002_MAE) > 0.01:
        raise RuntimeError(f'EXP-002 did not reproduce: {exp002["model"]["mae"]}')
    result['vs_exp002'] = paired_against_reference(predictions[ev.MODEL], calendar[ev.MODEL], data)

    cold = {d: bool(flag) for d, flag in labels.cold.items() if not pd.isna(flag)}
    label_missing = [str(d) for d, flag in labels.cold.items() if pd.isna(flag)]
    tables = {
        'exp003_vs_rte': day_type_table(predictions[ev.MODEL], data.consommation,
                                        data.prevision_j1, cold),
        'exp003_vs_rte_star': day_type_table(predictions[ev.MODEL], data.consommation,
                                             frame[RTE_STAR], cold),
        'exp002_vs_rte': day_type_table(calendar[ev.MODEL], data.consommation,
                                        data.prevision_j1, cold),
        'exp002_vs_rte_star': day_type_table(calendar[ev.MODEL], data.consommation,
                                             frame[RTE_STAR], cold)}
    for name, table in tables.items():
        print(name)
        print(table.round(2).to_string())
    result['day_types'] = tables['exp003_vs_rte'].to_dict(orient='index')
    result['day_types_rte_star'] = tables['exp003_vs_rte_star'].to_dict(orient='index')
    result['cold'] = 'D9 SYNOP labels, 12 stations, 4.84 C (labels.synop), applied in this script'
    result['day_type_tables'] = {k: v.to_dict(orient='index') for k, v in tables.items()}
    result['n_label_days'] = len(labels)
    result['n_cold_days'] = sum(cold.values())
    result['label_missing_days'] = label_missing
    result['weather_status_days'] = status
    result['n_backup_slots'] = int(flagged.sum())
    result['backup_days'] = [str(d) for d in flagged_days]

    monthly = pd.concat({
        'exp003': monthly_diagnostic(frame[[ev.MODEL, ev.ACTUAL, ev.RTE, RTE_STAR]].rename(
            columns={ev.RTE: 'rte_d1'}))}, axis=1)
    print(monthly.to_string())
    result['monthly_exp003_vs_rte'] = json.loads(monthly.to_json())
    old = frame.assign(**{ev.MODEL: calendar[ev.MODEL]})
    print('EXP-002 monthly')
    print(monthly_diagnostic(old[[ev.MODEL, ev.ACTUAL, ev.RTE]]).to_string())

    both = pd.DataFrame({'exp003': predictions[ev.MODEL], 'exp002': calendar[ev.MODEL],
                         ev.ACTUAL: data.consommation, ev.RTE: data.prevision_j1}).dropna()
    both_days = ev.local_dates(both.index)
    daily = (both[['exp003', 'exp002']].sub(both[ev.ACTUAL], axis=0).abs()
             .groupby(both_days).mean())
    cold_days = [d for d in daily.index if cold.get(d, False)]
    result['cold_days_descriptive'] = {
        'n_days': len(cold_days),
        'mean_daily_mae_exp003': float(daily.loc[cold_days, 'exp003'].mean()),
        'mean_daily_mae_exp002': float(daily.loc[cold_days, 'exp002'].mean())}
    print('cold days, mean daily MAE', result['cold_days_descriptive'])

    if '--dry' in sys.argv:
        print(json.dumps({k: result[k] for k in ('model', 'rte', RTE_STAR, 'bootstrap_7',
                                                 'rte_star_bootstrap_7', 'vs_exp002')},
                         indent=2, default=str))
        return
    result['mlflow_run_id'] = log_backtest(
        result, FIRST, LAST, exp='EXP-003', frame=frame,
        cold=cold, reference=calendar[ev.MODEL], reference_name='EXP-002', params={
            'model': 'OLS per local half-hour slot (48 models), calendar plus HDD and CDD',
            'features': (f'dow (holiday as Sunday), Fourier order 3, bridge, christmas_week, '
                         f'hdd=max(0,{HEATING_BASE_C}-T), cdd=max(0,T-{COOLING_BASE_C}), '
                         'T = equal-weight mean of 12 GFS points, run selected by D11'),
            'degree_bases': f'fixed a priori, heating {HEATING_BASE_C} C, cooling {COOLING_BASE_C} C',
            'training_start': TRAINING_START, 'refit': 'weekly, Monday cutoff, expanding window',
            'weather_missing': 'left out of the fit; forecast by the EXP-002 calendar model, flagged',
            'seed': 'none (deterministic least squares)', 'bootstrap_seed': ev.BOOTSTRAP_SEED,
            'reference': 'EXP-002',
            'source_dataset': ','.join(f'{n}:{c}' for n, c in sources) + ';gfs:raw.gfs_days,raw.gfs_points TMP',
            'cold_labels': 'D9 SYNOP, 12 stations, 4.84 C, labels.synop',
            'rte_star': 'a2: prior full Paris calendar year mean error by local hour'})
    stamp = int(time.time() * 1000)
    metrics = {'n_backup_slots': flagged.sum(), 'n_label_missing_days': len(label_missing),
               'vs_exp002_n_slots': result['vs_exp002']['n_slots'],
               'vs_exp002_reference_mae': result['vs_exp002']['reference_mae']}
    for name, label in (('exp003_vs_rte', 'rte_d1'), ('exp003_vs_rte_star', 'rte_star')):
        for kind in ('ordinary', 'cold', 'cold_any', 'holiday', 'bridge', 'dst'):
            if kind in tables[name].index:
                row = tables[name].loc[kind]
                metrics[f'lab_{kind}_model_vs_{label}'] = row[ev.MODEL]
                metrics[f'lab_{kind}_{label}'] = row[ev.RTE]
                metrics[f'lab_{kind}_n_days'] = row['n_days']
    for block in (7, 14):
        for key, value in result['vs_exp002'][f'bootstrap_{block}'].items():
            if key != 'verdict':
                metrics[f'vs_exp002_bootstrap_{block}_{key}'] = value
    mlflow = f'{mlflow_tracking_uri()}/api/2.0/mlflow'
    response = requests.post(f'{mlflow}/runs/log-batch', json={
        'run_id': result['mlflow_run_id'],
        'metrics': [{'key': k, 'value': float(v), 'timestamp': stamp, 'step': 1}
                    for k, v in metrics.items()]}, timeout=30)
    response.raise_for_status()
    status_response = requests.get(f'{mlflow}/runs/get', params={'run_id': result['mlflow_run_id']},
                                   timeout=30)
    status_response.raise_for_status()
    result['mlflow_status'] = status_response.json()['run']['info']['status']
    if result['mlflow_status'] != 'FINISHED':
        raise RuntimeError('MLflow did not confirm the completed run')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    predictions.to_csv(OUTPUT / 'backtest_predictions.csv', index_label='value_time_utc')
    (OUTPUT / 'results.json').write_text(json.dumps(result, indent=2, default=str),
                                         encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('model', 'rte', RTE_STAR, 'bootstrap_7', 'bootstrap_14',
                                             'rte_star_bootstrap_7', 'rte_star_bootstrap_14',
                                             'vs_exp002', 'mlflow_run_id', 'mlflow_status')},
                     indent=2, default=str))
    print(asdict(ev.score(frame[[ev.MODEL, ev.ACTUAL, ev.RTE]]).model))


if __name__ == '__main__':
    main()
