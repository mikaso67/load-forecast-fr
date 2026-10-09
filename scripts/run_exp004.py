"""EXP-004: EXP-003 plus lagged consumption (M3 A), validation year 2024 only."""

import json
import sys
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd
import requests
from run_exp002 import TRAINING_START, load, paired_against_reference
from run_exp003 import SOURCE_QUERY, WEATHER_FIRST, day_type_table, monthly_diagnostic

from load_forecast import evaluation as ev
from load_forecast.backtest import log_backtest, score_backtest, walk_forward
from load_forecast.baseline import FIRST, LAST
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.labels import read_labels
from load_forecast.lag_model import BACKUP_FLAG, lagged_ols_features
from load_forecast.linear_model import SlotOLS, calendar_ols_features
from load_forecast.rte_reference import RTE_STAR, prior_year_hourly_reference
from load_forecast.temperature_model import (
    COOLING_BASE_C,
    HEATING_BASE_C,
    temperature_ols_features,
    with_calendar_backup,
)
from load_forecast.tracking import mlflow_tracking_uri
from load_forecast.weather_features import read_temperature

OUTPUT = Path('data/experiments/EXP-004')
EXP003_MAE = 1719.06


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

    calendar = walk_forward(data.consommation, FIRST, LAST, SlotOLS,
                            builder=calendar_ols_features, training_start=TRAINING_START)
    weather = walk_forward(data.consommation, FIRST, LAST, SlotOLS,
                           builder=temperature_ols_features(temperature),
                           training_start=TRAINING_START)
    exp003 = with_calendar_backup(weather, calendar)
    frame003 = exp003.assign(actual=data.consommation, rte_d1=data.prevision_j1)
    if abs(ev.score(frame003[[ev.MODEL, ev.ACTUAL, ev.RTE]]).model.mae - EXP003_MAE) > 0.01:
        raise RuntimeError('EXP-003 did not reproduce')
    lagged = walk_forward(data.consommation, FIRST, LAST, SlotOLS,
                          builder=lagged_ols_features(temperature), training_start=TRAINING_START)
    predictions = with_calendar_backup(lagged, exp003, flag=BACKUP_FLAG)
    flagged = predictions[BACKUP_FLAG]
    flagged_days = sorted(set(ev.local_dates(predictions.index[flagged])))
    print('slots on the EXP-003 backup', int(flagged.sum()), '| days', len(flagged_days),
          [str(d) for d in flagged_days])
    if predictions[ev.MODEL].isna().any():
        raise RuntimeError('Forecast slots without any model')

    frame = predictions.assign(actual=data.consommation, rte_d1=data.prevision_j1)
    frame[RTE_STAR] = prior_year_hourly_reference(data.consommation, data.prevision_j1,
                                                 frame[ev.RTE])
    result = score_backtest(predictions, data.consommation, data.prevision_j1, frame[RTE_STAR])
    result['vs_exp003'] = paired_against_reference(predictions[ev.MODEL], exp003[ev.MODEL], data)

    cold = {d: bool(flag) for d, flag in labels.cold.items() if not pd.isna(flag)}
    label_missing = [str(d) for d, flag in labels.cold.items() if pd.isna(flag)]
    tables = {
        'exp004_vs_rte': day_type_table(predictions[ev.MODEL], data.consommation,
                                        data.prevision_j1, cold),
        'exp004_vs_rte_star': day_type_table(predictions[ev.MODEL], data.consommation,
                                             frame[RTE_STAR], cold),
        'exp003_vs_rte': day_type_table(exp003[ev.MODEL], data.consommation,
                                        data.prevision_j1, cold)}
    for name, table in tables.items():
        print(name)
        print(table.round(2).to_string())
    result['day_types'] = tables['exp004_vs_rte'].to_dict(orient='index')
    result['day_types_rte_star'] = tables['exp004_vs_rte_star'].to_dict(orient='index')
    result['day_type_tables'] = {k: v.to_dict(orient='index') for k, v in tables.items()}
    result['cold'] = 'D9 SYNOP labels, 12 stations, 4.84 C (labels.synop), applied in this script'
    result['n_label_days'] = len(labels)
    result['n_cold_days'] = sum(cold.values())
    result['label_missing_days'] = label_missing
    result['n_backup_slots'] = int(flagged.sum())
    result['backup_days'] = [str(d) for d in flagged_days]

    monthly = {}
    for name, forecast in (('exp004', predictions), ('exp003', exp003)):
        monthly[name] = monthly_diagnostic(pd.DataFrame({
            ev.MODEL: forecast[ev.MODEL], ev.ACTUAL: data.consommation, ev.RTE: data.prevision_j1}))
    table = pd.concat(monthly, axis=1)
    print(table.to_string())
    result['monthly'] = json.loads(table.to_json())

    both = pd.DataFrame({'exp004': predictions[ev.MODEL], 'exp003': exp003[ev.MODEL],
                         ev.ACTUAL: data.consommation, ev.RTE: data.prevision_j1}).dropna()
    both_days = ev.local_dates(both.index)
    daily = both[['exp004', 'exp003']].sub(both[ev.ACTUAL], axis=0).abs().groupby(both_days).mean()
    cold_days = [d for d in daily.index if cold.get(d, False)]
    result['cold_days_descriptive'] = {
        'n_days': len(cold_days),
        'mean_daily_mae_exp004': float(daily.loc[cold_days, 'exp004'].mean()),
        'mean_daily_mae_exp003': float(daily.loc[cold_days, 'exp003'].mean())}
    print('cold days, mean daily MAE', result['cold_days_descriptive'])

    keys = ('model', 'rte', RTE_STAR, 'bootstrap_7', 'bootstrap_14', 'rte_star_bootstrap_7',
            'rte_star_bootstrap_14', 'vs_exp003')
    if '--dry' in sys.argv:
        print(json.dumps({k: result[k] for k in keys}, indent=2, default=str))
        return
    result['mlflow_run_id'] = log_backtest(
        result, FIRST, LAST, exp='EXP-004', frame=frame,
        cold=cold, reference=exp003[ev.MODEL], reference_name='EXP-003', params={
            'model': 'OLS per local half-hour slot (48 models), calendar plus HDD, CDD and lags',
            'features': (f'EXP-003 features (hdd=max(0,{HEATING_BASE_C}-T), '
                         f'cdd=max(0,T-{COOLING_BASE_C})) plus lag_d2, lag_d7, lag_last '
                         '(D-1 09:30 local slot) and special_d2, special_d7, special_last '
                         '(holiday or bridge on the lag day)'),
            'lag_matching': 'Paris wall-clock slot to UTC, never by row offset (M1)',
            'missing_lag_rule': 'any missing lag: whole day without lag features, forecast by '
                                'EXP-003 and flagged backup_exp003',
            'training_start': TRAINING_START, 'refit': 'weekly, Monday cutoff, expanding window',
            'seed': 'none (deterministic least squares)', 'bootstrap_seed': ev.BOOTSTRAP_SEED,
            'reference': 'EXP-003',
            'source_dataset': ','.join(f'{n}:{c}' for n, c in sources)
                              + ';gfs:raw.gfs_days,raw.gfs_points TMP',
            'cold_labels': 'D9 SYNOP, 12 stations, 4.84 C, labels.synop',
            'rte_star': 'a2: prior full Paris calendar year mean error by local hour'})
    stamp = int(time.time() * 1000)
    metrics = {'n_backup_slots': flagged.sum(), 'n_backup_days': len(flagged_days),
               'n_label_missing_days': len(label_missing),
               'vs_exp003_n_slots': result['vs_exp003']['n_slots'],
               'vs_exp003_reference_mae': result['vs_exp003']['reference_mae']}
    for name, label in (('exp004_vs_rte', 'rte_d1'), ('exp004_vs_rte_star', 'rte_star')):
        for kind in ('ordinary', 'cold', 'cold_any', 'holiday', 'bridge', 'dst'):
            if kind in tables[name].index:
                row = tables[name].loc[kind]
                metrics[f'lab_{kind}_model_vs_{label}'] = row[ev.MODEL]
                metrics[f'lab_{kind}_{label}'] = row[ev.RTE]
                metrics[f'lab_{kind}_n_days'] = row['n_days']
    for block in (7, 14):
        for key, value in result['vs_exp003'][f'bootstrap_{block}'].items():
            if key != 'verdict':
                metrics[f'vs_exp003_bootstrap_{block}_{key}'] = value
    mlflow = f'{mlflow_tracking_uri()}/api/2.0/mlflow'
    requests.post(f'{mlflow}/runs/log-batch', json={
        'run_id': result['mlflow_run_id'],
        'metrics': [{'key': k, 'value': float(v), 'timestamp': stamp, 'step': 1}
                    for k, v in metrics.items()]}, timeout=30).raise_for_status()
    got = requests.get(f'{mlflow}/runs/get', params={'run_id': result['mlflow_run_id']}, timeout=30)
    got.raise_for_status()
    result['mlflow_status'] = got.json()['run']['info']['status']
    if result['mlflow_status'] != 'FINISHED':
        raise RuntimeError('MLflow did not confirm the completed run')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    predictions.to_csv(OUTPUT / 'backtest_predictions.csv', index_label='value_time_utc')
    (OUTPUT / 'results.json').write_text(json.dumps(result, indent=2, default=str),
                                         encoding='utf-8')
    print(json.dumps({k: result[k] for k in (*keys, 'mlflow_run_id', 'mlflow_status')},
                     indent=2, default=str))


if __name__ == '__main__':
    main()
