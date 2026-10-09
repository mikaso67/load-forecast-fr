"""EXP-008: one global LightGBM model on the EXP-005 features, validation year 2024 only."""

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
from run_exp007 import BACKUP_FLAG_007, EXP005_MAE, FILTER_MODE

from load_forecast import evaluation as ev
from load_forecast.backtest import log_backtest, score_backtest, walk_forward
from load_forecast.baseline import FIRST, LAST
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.experiment_reports import committed_report
from load_forecast.gam_model import lagged_gam_features
from load_forecast.gbm_model import GBMConfig, booster_params, walk_forward_global
from load_forecast.kalman import walk_forward_kalman
from load_forecast.labels import read_labels
from load_forecast.lag_model import BACKUP_FLAG, lagged_ols_features
from load_forecast.linear_model import SlotOLS, calendar_ols_features
from load_forecast.rte_reference import RTE_STAR, prior_year_hourly_reference
from load_forecast.temperature_model import temperature_ols_features, with_calendar_backup
from load_forecast.tracking import mlflow_tracking_uri
from load_forecast.weather_features import read_temperature

OUTPUT = Path('data/experiments/EXP-008')
EXP007_MAE = 885.32
# Chosen on 2023 only by scripts/select_lightgbm.py and committed in EXP-008.md before this run.
CONFIG = GBMConfig(learning_rate=0.1, num_leaves=15, min_data_in_leaf=100, rounds=6400)
PREREGISTRATION_COMMIT = 'e24c2963bb46bd60c5bb6208f2de0af73c700763'


def check_preregistration() -> None:
    """Refuse to run unless the 2023 choice is in the history and names the config used here."""
    ancestor = subprocess.run(['git', 'merge-base', '--is-ancestor', PREREGISTRATION_COMMIT, 'HEAD'],
                              check=False)
    if ancestor.returncode != 0:
        raise RuntimeError('The pre-registration commit is not in the history of HEAD')
    text = committed_report(PREREGISTRATION_COMMIT, 'EXP-008.md')
    if f'The choice is **`{CONFIG.label()}`**' not in text:
        raise RuntimeError('The committed EXP-008.md does not record this configuration')


def cold_extrapolation(temperature: pd.DataFrame, cold: dict, errors: pd.DataFrame) -> dict:
    """Is each 2024 cold day colder than every day before the refit Monday of its week?"""
    days = pd.Series(ev.local_dates(temperature.index), index=temperature.index)
    daily_min = temperature['temperature_c'].groupby(days).min()
    rows = []
    for day, flag in cold.items():
        if not flag or day not in daily_min.index or not FIRST <= day <= LAST:
            continue
        monday = day - timedelta(days=day.weekday())
        seen = daily_min.loc[[d for d in daily_min.index if d < monday]]
        rows.append({'day': str(day), 'day_min_temperature_c': float(daily_min[day]),
                     'training_min_temperature_c': float(seen.min()),
                     'below_training_range': bool(daily_min[day] < seen.min())})
    slot_days = pd.Series(ev.local_dates(errors.index), index=errors.index)
    cold_slots = slot_days.map(lambda d: bool(cold.get(d, False)))
    return {'n_cold_days': len(rows),
            'n_below_training_range': sum(r['below_training_range'] for r in rows),
            'mean_signed_error_on_cold_days_mw': errors.loc[cold_slots].mean().to_dict(),
            'days': rows}


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
    print('OLS chain 1/3 done', flush=True)
    exp003 = with_calendar_backup(weather, calendar)
    lagged = walk_forward(data.consommation, FIRST, LAST, SlotOLS,
                          builder=lagged_ols_features(temperature), training_start=TRAINING_START)
    exp004 = with_calendar_backup(lagged, exp003, flag=BACKUP_FLAG)
    print('OLS chains done', flush=True)

    began = time.perf_counter()
    kalman = walk_forward_kalman(data.consommation, FIRST, LAST, make_gam1,
                                 builder=lagged_gam_features(temperature),
                                 training_start=TRAINING_START, modes=(FILTER_MODE,))
    kalman_seconds = round(time.perf_counter() - began, 1)
    print('Kalman runner seconds', kalman_seconds, flush=True)
    exp005 = with_calendar_backup(kalman.gam, exp004, flag=BACKUP_FLAG_005)
    exp007 = with_calendar_backup(kalman.corrected[FILTER_MODE], exp005, flag=BACKUP_FLAG_007)
    for name, forecast, expected in (('EXP-003', exp003, EXP003_MAE), ('EXP-004', exp004, EXP004_MAE),
                                     ('EXP-005', exp005, EXP005_MAE),
                                     ('EXP-007', exp007, EXP007_MAE)):
        frame = forecast.assign(actual=data.consommation, rte_d1=data.prevision_j1)
        mae = ev.score(frame[[ev.MODEL, ev.ACTUAL, ev.RTE]]).model.mae
        if abs(mae - expected) > 0.01:
            raise RuntimeError(f'{name} did not reproduce: {mae:.2f} instead of {expected}')
        print(name, 'reproduced, MAE', round(mae, 2))

    began = time.perf_counter()

    def progress(refit, model) -> None:
        print('refit', refit, 'trees', model.booster_.num_trees(),
              'elapsed s', round(time.perf_counter() - began), flush=True)

    raw = walk_forward_global(data.consommation, FIRST, LAST, CONFIG,
                              builder=lagged_gam_features(temperature),
                              training_start=TRAINING_START, on_refit=progress)[CONFIG.rounds]
    seconds = round(time.perf_counter() - began, 1)
    predictions = with_calendar_backup(raw, exp005, flag=BACKUP_FLAG_005)
    flagged = predictions[BACKUP_FLAG_005]
    flagged_days = sorted(set(ev.local_dates(predictions.index[flagged])))
    print('LightGBM walk-forward seconds', seconds, '| slots on the EXP-005 backup',
          int(flagged.sum()), '| days', [str(d) for d in flagged_days])
    if predictions[ev.MODEL].isna().any():
        raise RuntimeError('Forecast slots without any model')

    frame = predictions.assign(actual=data.consommation, rte_d1=data.prevision_j1)
    frame[RTE_STAR] = prior_year_hourly_reference(data.consommation, data.prevision_j1,
                                                 frame[ev.RTE])
    result = score_backtest(predictions, data.consommation, data.prevision_j1, frame[RTE_STAR])
    result['vs_exp005'] = paired_against_reference(predictions[ev.MODEL], exp005[ev.MODEL], data)
    result['vs_exp007'] = paired_against_reference(predictions[ev.MODEL], exp007[ev.MODEL], data)
    result['lightgbm_seconds'] = seconds
    result['config'] = CONFIG.label()
    result['preregistration_commit'] = PREREGISTRATION_COMMIT

    cold = {d: bool(flag) for d, flag in labels.cold.items() if not pd.isna(flag)}
    label_missing = [str(d) for d, flag in labels.cold.items() if pd.isna(flag)]
    models = {'exp008': predictions, 'exp007': exp007, 'exp005': exp005}
    tables = {f'{name}_vs_rte': day_type_table(f[ev.MODEL], data.consommation,
                                               data.prevision_j1, cold)
              for name, f in models.items()}
    tables['exp008_vs_rte_star'] = day_type_table(predictions[ev.MODEL], data.consommation,
                                                  frame[RTE_STAR], cold)
    for name, table in tables.items():
        print(name)
        print(table.round(2).to_string())
    result['day_types'] = tables['exp008_vs_rte'].to_dict(orient='index')
    result['day_types_rte_star'] = tables['exp008_vs_rte_star'].to_dict(orient='index')
    result['day_type_tables'] = {k: v.to_dict(orient='index') for k, v in tables.items()}
    result['cold'] = 'D9 SYNOP labels, 12 stations, 4.84 C (labels.synop), applied in this script'
    result['n_label_days'] = len(labels)
    result['n_cold_days'] = sum(cold.values())
    result['label_missing_days'] = label_missing
    result['n_backup_slots'] = int(flagged.sum())
    result['backup_days'] = [str(d) for d in flagged_days]

    monthly = {name: monthly_diagnostic(pd.DataFrame({
        ev.MODEL: f[ev.MODEL], ev.ACTUAL: data.consommation, ev.RTE: data.prevision_j1}))
        for name, f in models.items()}
    table = pd.concat(monthly, axis=1)
    print(table.to_string())
    result['monthly'] = json.loads(table.to_json())

    both = pd.DataFrame({name: f[ev.MODEL] for name, f in models.items()}
                        | {ev.ACTUAL: data.consommation, ev.RTE: data.prevision_j1}).dropna()
    errors = both[list(models)].sub(both[ev.ACTUAL], axis=0)
    daily = errors.abs().groupby(ev.local_dates(both.index)).mean()
    cold_days = [d for d in daily.index if cold.get(d, False)]
    result['cold_days_descriptive'] = {
        'n_days': len(cold_days),
        **{f'mean_daily_mae_{name}': float(daily.loc[cold_days, name].mean()) for name in models}}
    print('cold days, mean daily MAE', result['cold_days_descriptive'])
    result['cold_extrapolation'] = cold_extrapolation(temperature, cold, errors)
    print(json.dumps({k: v for k, v in result['cold_extrapolation'].items() if k != 'days'},
                     indent=2))

    keys = ('model', 'rte', RTE_STAR, 'bootstrap_7', 'bootstrap_14', 'rte_star_bootstrap_7',
            'rte_star_bootstrap_14', 'vs_exp005', 'vs_exp007')
    if '--dry' in sys.argv:
        print(json.dumps({k: result[k] for k in keys}, indent=2, default=str))
        return
    result['mlflow_run_id'] = log_backtest(
        result, FIRST, LAST, exp='EXP-008', frame=frame,
        cold=cold, reference=exp005[ev.MODEL], reference_name='EXP-005', params={
            'model': 'one global LightGBM booster for all slots, local half-hour slot as a '
                     'numeric feature, objective l1',
            'features': 'EXP-005 lagged_gam_features frame (calendar, GFS temperature, lag_d2, '
                        'lag_d7, lag_last, special_*) plus slot',
            **{k: v for k, v in booster_params(CONFIG).items() if k != 'metric'},
            'rounds': CONFIG.rounds, 'config_label': CONFIG.label(),
            'preregistration_commit': PREREGISTRATION_COMMIT,
            'tuning_rule': 'grid of 40 on the 2023 inner walk-forward (scripts/select_lightgbm.py), '
                           'frozen and committed before 2024',
            'missing_rule': 'missing lag or weather: NaN, EXP-005 forecast, flagged backup_exp005',
            'training_start': TRAINING_START, 'refit': 'weekly, Monday cutoff, expanding window',
            'seed': 'lightgbm seed 20240, deterministic', 'bootstrap_seed': ev.BOOTSTRAP_SEED,
            'reference': 'EXP-005', 'gam_smoothing_weight_of_the_reference': SMOOTHING_WEIGHT,
            'source_dataset': ','.join(f'{n}:{c}' for n, c in sources)
                              + ';gfs:raw.gfs_days,raw.gfs_points TMP',
            'cold_labels': 'D9 SYNOP, 12 stations, 4.84 C, labels.synop',
            'rte_star': 'a2: prior full Paris calendar year mean error by local hour'})
    stamp = int(time.time() * 1000)
    metrics = {'n_backup_slots': flagged.sum(), 'n_backup_days': len(flagged_days),
               'n_label_missing_days': len(label_missing), 'lightgbm_seconds': seconds,
               'n_cold_days_below_training_range':
                   result['cold_extrapolation']['n_below_training_range']}
    for ref in ('exp005', 'exp007'):
        metrics[f'vs_{ref}_n_slots'] = result[f'vs_{ref}']['n_slots']
        metrics[f'vs_{ref}_reference_mae'] = result[f'vs_{ref}']['reference_mae']
        for block in (7, 14):
            for key, value in result[f'vs_{ref}'][f'bootstrap_{block}'].items():
                if key != 'verdict':
                    metrics[f'vs_{ref}_bootstrap_{block}_{key}'] = value
    for name, label in (('exp008_vs_rte', 'rte_d1'), ('exp008_vs_rte_star', 'rte_star')):
        for kind in ('ordinary', 'cold', 'cold_any', 'holiday', 'bridge', 'dst'):
            if kind in tables[name].index:
                row = tables[name].loc[kind]
                metrics[f'lab_{kind}_model_vs_{label}'] = row[ev.MODEL]
                metrics[f'lab_{kind}_{label}'] = row[ev.RTE]
                metrics[f'lab_{kind}_n_days'] = row['n_days']
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
    print('kalman runner seconds', kalman_seconds)


if __name__ == '__main__':
    main()
