"""EXP-007: EXP-005 GAM plus an online Kalman correction (M7 B), validation year 2024 only."""

import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd
import requests
from run_exp002 import TRAINING_START, load, paired_against_reference
from run_exp003 import SOURCE_QUERY, WEATHER_FIRST, day_type_table, monthly_diagnostic
from run_exp005 import BACKUP_FLAG_005, EXP003_MAE, EXP004_MAE, SMOOTHING_WEIGHT
from run_exp006 import make_gam1

from load_forecast import evaluation as ev
from load_forecast.backtest import log_backtest, score_backtest, walk_forward
from load_forecast.baseline import FIRST, LAST
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.experiment_reports import committed_report
from load_forecast.gam_model import lagged_gam_features
from load_forecast.kalman import walk_forward_kalman
from load_forecast.labels import read_labels
from load_forecast.lag_model import BACKUP_FLAG, lagged_ols_features
from load_forecast.linear_model import SlotOLS, calendar_ols_features
from load_forecast.rte_reference import RTE_STAR, prior_year_hourly_reference
from load_forecast.temperature_model import temperature_ols_features, with_calendar_backup
from load_forecast.tracking import mlflow_tracking_uri
from load_forecast.weather_features import read_temperature

OUTPUT = Path('data/experiments/EXP-007')
EXP005_MAE = 911.85
BACKUP_FLAG_007 = 'backup_exp005'
# Chosen on 2023 only by scripts/select_kalman_mode.py and committed in EXP-007.md before this run.
FILTER_MODE = 'restart'
PREREGISTRATION_COMMIT = 'ae66a8b528a645a75e77493326f61b9441d2b023'


def check_preregistration() -> None:
    """Refuse to run unless the 2023 choice is in the history and names the mode used here."""
    ancestor = subprocess.run(['git', 'merge-base', '--is-ancestor', PREREGISTRATION_COMMIT, 'HEAD'],
                              check=False)
    if ancestor.returncode != 0:
        raise RuntimeError('The pre-registration commit is not in the history of HEAD')
    text = committed_report(PREREGISTRATION_COMMIT, 'EXP-007.md')
    if f'the choice is **`{FILTER_MODE}`**' not in text:
        raise RuntimeError('The committed EXP-007.md does not record this filter mode')


def main() -> None:
    check_preregistration()
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
    lagged = walk_forward(data.consommation, FIRST, LAST, SlotOLS,
                          builder=lagged_ols_features(temperature), training_start=TRAINING_START)
    exp004 = with_calendar_backup(lagged, exp003, flag=BACKUP_FLAG)

    began = time.perf_counter()
    kalman = walk_forward_kalman(data.consommation, FIRST, LAST, make_gam1,
                                 builder=lagged_gam_features(temperature),
                                 training_start=TRAINING_START, modes=(FILTER_MODE,))
    seconds = round(time.perf_counter() - began, 1)
    exp005 = with_calendar_backup(kalman.gam, exp004, flag=BACKUP_FLAG_005)
    for name, forecast, expected in (('EXP-003', exp003, EXP003_MAE), ('EXP-004', exp004, EXP004_MAE),
                                     ('EXP-005', exp005, EXP005_MAE)):
        frame = forecast.assign(actual=data.consommation, rte_d1=data.prevision_j1)
        mae = ev.score(frame[[ev.MODEL, ev.ACTUAL, ev.RTE]]).model.mae
        if abs(mae - expected) > 0.01:
            raise RuntimeError(f'{name} did not reproduce: {mae:.2f} instead of {expected}')
        print(name, 'reproduced, MAE', round(mae, 2))

    predictions = with_calendar_backup(kalman.corrected[FILTER_MODE], exp005, flag=BACKUP_FLAG_007)
    flagged = predictions[BACKUP_FLAG_007]
    flagged_days = sorted(set(ev.local_dates(predictions.index[flagged])))
    print('walk-forward seconds', seconds, '| slots on the EXP-005 backup', int(flagged.sum()),
          '| days', [str(d) for d in flagged_days])
    if predictions[ev.MODEL].isna().any():
        raise RuntimeError('Forecast slots without any model')
    correction = (predictions[ev.MODEL] - exp005[ev.MODEL]).loc[~flagged]
    print('mean abs correction (MW)', round(float(correction.abs().mean()), 1),
          '| mean correction', round(float(correction.mean()), 1))

    frame = predictions.assign(actual=data.consommation, rte_d1=data.prevision_j1)
    frame[RTE_STAR] = prior_year_hourly_reference(data.consommation, data.prevision_j1,
                                                 frame[ev.RTE])
    result = score_backtest(predictions, data.consommation, data.prevision_j1, frame[RTE_STAR])
    result['vs_exp005'] = paired_against_reference(predictions[ev.MODEL], exp005[ev.MODEL], data)
    result['kalman_seconds'] = seconds
    result['filter_mode'] = FILTER_MODE
    result['preregistration_commit'] = PREREGISTRATION_COMMIT
    result['variance_fits'] = [{**f, 'refit': str(f['refit'])} for f in kalman.fits]
    result['mean_abs_correction_mw'] = float(correction.abs().mean())
    result['mean_correction_mw'] = float(correction.mean())

    cold = {d: bool(flag) for d, flag in labels.cold.items() if not pd.isna(flag)}
    label_missing = [str(d) for d, flag in labels.cold.items() if pd.isna(flag)]
    tables = {
        'exp007_vs_rte': day_type_table(predictions[ev.MODEL], data.consommation,
                                        data.prevision_j1, cold),
        'exp007_vs_rte_star': day_type_table(predictions[ev.MODEL], data.consommation,
                                             frame[RTE_STAR], cold),
        'exp005_vs_rte': day_type_table(exp005[ev.MODEL], data.consommation,
                                        data.prevision_j1, cold)}
    for name, table in tables.items():
        print(name)
        print(table.round(2).to_string())
    result['day_types'] = tables['exp007_vs_rte'].to_dict(orient='index')
    result['day_types_rte_star'] = tables['exp007_vs_rte_star'].to_dict(orient='index')
    result['day_type_tables'] = {k: v.to_dict(orient='index') for k, v in tables.items()}
    result['cold'] = 'D9 SYNOP labels, 12 stations, 4.84 C (labels.synop), applied in this script'
    result['n_label_days'] = len(labels)
    result['n_cold_days'] = sum(cold.values())
    result['label_missing_days'] = label_missing
    result['n_backup_slots'] = int(flagged.sum())
    result['backup_days'] = [str(d) for d in flagged_days]

    monthly = {name: monthly_diagnostic(pd.DataFrame({
        ev.MODEL: forecast[ev.MODEL], ev.ACTUAL: data.consommation, ev.RTE: data.prevision_j1}))
        for name, forecast in (('exp007', predictions), ('exp005', exp005))}
    table = pd.concat(monthly, axis=1)
    print(table.to_string())
    result['monthly'] = json.loads(table.to_json())

    both = pd.DataFrame({'exp007': predictions[ev.MODEL], 'exp005': exp005[ev.MODEL],
                         ev.ACTUAL: data.consommation, ev.RTE: data.prevision_j1}).dropna()
    daily = both[['exp007', 'exp005']].sub(both[ev.ACTUAL], axis=0).abs().groupby(
        ev.local_dates(both.index)).mean()
    cold_days = [d for d in daily.index if cold.get(d, False)]
    result['cold_days_descriptive'] = {
        'n_days': len(cold_days),
        'mean_daily_mae_exp007': float(daily.loc[cold_days, 'exp007'].mean()),
        'mean_daily_mae_exp005': float(daily.loc[cold_days, 'exp005'].mean())}
    print('cold days, mean daily MAE', result['cold_days_descriptive'])

    keys = ('model', 'rte', RTE_STAR, 'bootstrap_7', 'bootstrap_14', 'rte_star_bootstrap_7',
            'rte_star_bootstrap_14', 'vs_exp005')
    if '--dry' in sys.argv:
        print(json.dumps({k: result[k] for k in keys}, indent=2, default=str))
        return
    result['mlflow_run_id'] = log_backtest(
        result, FIRST, LAST, exp='EXP-007', frame=frame,
        cold=cold, reference=exp005[ev.MODEL], reference_name='EXP-005', params={
            'model': 'EXP-005 GAM per local half-hour slot plus a Kalman filter on its residual '
                     '(random-walk coefficients on 1 and three standardized GAM partial effects)',
            'filter_mode': FILTER_MODE, 'preregistration_commit': PREREGISTRATION_COMMIT,
            'update_rule': 'filter updated with the full day D-2 only (checklist item 7)',
            'variances': 'per slot Gaussian maximum likelihood, re-estimated at the first weekly '
                         'refit of each calendar quarter on days known at that cutoff, D-2 rule',
            'prior_covariance': 1e4, 'max_iterations': 100,
            'smoothing_weight': SMOOTHING_WEIGHT,
            'missing_rule': 'missing lag or weather: no update, EXP-005 forecast, flagged '
                            'backup_exp005',
            'training_start': TRAINING_START, 'refit': 'weekly, Monday cutoff, expanding window',
            'seed': 'none (deterministic)', 'bootstrap_seed': ev.BOOTSTRAP_SEED,
            'reference': 'EXP-005',
            'source_dataset': ','.join(f'{n}:{c}' for n, c in sources)
                              + ';gfs:raw.gfs_days,raw.gfs_points TMP',
            'cold_labels': 'D9 SYNOP, 12 stations, 4.84 C, labels.synop',
            'rte_star': 'a2: prior full Paris calendar year mean error by local hour'})
    stamp = int(time.time() * 1000)
    metrics = {'n_backup_slots': flagged.sum(), 'n_backup_days': len(flagged_days),
               'n_label_missing_days': len(label_missing), 'kalman_seconds': seconds,
               'mean_abs_correction_mw': result['mean_abs_correction_mw'],
               'vs_exp005_n_slots': result['vs_exp005']['n_slots'],
               'vs_exp005_reference_mae': result['vs_exp005']['reference_mae']}
    for name, label in (('exp007_vs_rte', 'rte_d1'), ('exp007_vs_rte_star', 'rte_star')):
        for kind in ('ordinary', 'cold', 'cold_any', 'holiday', 'bridge', 'dst'):
            if kind in tables[name].index:
                row = tables[name].loc[kind]
                metrics[f'lab_{kind}_model_vs_{label}'] = row[ev.MODEL]
                metrics[f'lab_{kind}_{label}'] = row[ev.RTE]
                metrics[f'lab_{kind}_n_days'] = row['n_days']
    for block in (7, 14):
        for key, value in result['vs_exp005'][f'bootstrap_{block}'].items():
            if key != 'verdict':
                metrics[f'vs_exp005_bootstrap_{block}_{key}'] = value
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
