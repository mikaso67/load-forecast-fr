"""EXP-009: ML-Poly aggregation of EXP-004, EXP-005, EXP-007 and EXP-008, validation year 2024 only."""

import hashlib
import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd
import requests
from expert_forecasts import EXPERTS, expert_forecasts
from run_exp002 import load, paired_against_reference
from run_exp003 import SOURCE_QUERY, day_type_table, monthly_diagnostic
from run_exp005 import EXP004_MAE
from run_exp007 import EXP005_MAE
from run_exp008 import EXP007_MAE
from select_aggregation import CACHE

from load_forecast import evaluation as ev
from load_forecast.aggregation import aggregate
from load_forecast.backtest import log_backtest, score_backtest
from load_forecast.baseline import FIRST, LAST
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.experiment_reports import committed_report
from load_forecast.labels import read_labels
from load_forecast.rte_reference import RTE_STAR, prior_year_hourly_reference
from load_forecast.tracking import mlflow_tracking_uri

OUTPUT = Path('data/experiments/EXP-009')
EXP008_MAE = 1113.94
EXPECTED = {'EXP-004': EXP004_MAE, 'EXP-005': EXP005_MAE, 'EXP-007': EXP007_MAE,
            'EXP-008': EXP008_MAE}
# Chosen on 2023 only by scripts/select_aggregation.py and committed in EXP-009.md before this run.
VARIANT = 'shared'
PREREGISTRATION_COMMIT = '6b9c1c23d4289c3d6df5f8691215fbea38cbbf4a'


def check_preregistration() -> None:
    """Refuse to run unless the 2023 choice is in the history and names the variant used here."""
    ancestor = subprocess.run(['git', 'merge-base', '--is-ancestor', PREREGISTRATION_COMMIT, 'HEAD'],
                              check=False)
    if ancestor.returncode != 0:
        raise RuntimeError('The pre-registration commit is not in the history of HEAD')
    text = committed_report(PREREGISTRATION_COMMIT, 'EXP-009.md')
    if f'the choice is **`{VARIANT}`**' not in text:
        raise RuntimeError('The committed EXP-009.md does not record this variant')


def mean_weights(weights: pd.DataFrame, days: list) -> dict:
    kept = weights.loc[weights.index.get_level_values('day').isin(days)]
    return {} if kept.empty else kept.mean().round(4).to_dict()


def main() -> None:
    check_preregistration()
    if not CACHE.exists():
        raise RuntimeError('The 2023 expert forecasts are missing: run scripts/select_aggregation.py')
    burn_in = pd.read_csv(CACHE, index_col=0, parse_dates=True)[list(EXPERTS)]
    data = load()
    with connect_database() as connection:
        labels = read_labels(connection, FIRST, LAST + timedelta(days=1))
        start = pd.Timestamp(FIRST, tz='Europe/Paris').tz_convert('UTC')
        end = pd.Timestamp(LAST + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
        sources = connection.execute(clean_query(SOURCE_QUERY), {'start': start, 'end': end}).fetchall()

    began = time.perf_counter()
    experts_2024, backups = expert_forecasts(data.consommation, FIRST, LAST)
    seconds = round(time.perf_counter() - began, 1)
    for name in EXPERTS:
        frame = pd.DataFrame({ev.MODEL: experts_2024[name], ev.ACTUAL: data.consommation,
                              ev.RTE: data.prevision_j1})
        mae = ev.score(frame).model.mae
        if abs(mae - EXPECTED[name]) > 0.01:
            raise RuntimeError(f'{name} did not reproduce: {mae:.2f} instead of {EXPECTED[name]}')
        print(name, 'reproduced, MAE', round(mae, 2), '| backup slots', backups[name], flush=True)

    experts = pd.concat([burn_in, experts_2024])
    if experts.index.has_duplicates or not experts.index.is_monotonic_increasing:
        raise RuntimeError('2023 and 2024 expert forecasts overlap or are unordered')
    aggregation = aggregate(experts, data.consommation, per_slot=VARIANT == 'per_slot')
    in_2024 = experts.index >= start
    predictions = aggregation.forecast.loc[in_2024].to_frame()
    in_2023 = ~in_2024
    burn = pd.DataFrame({'f': aggregation.forecast.loc[in_2023], 'y': data.consommation}).dropna()
    burn_mae = float((burn.f - burn.y).abs().mean())
    print('2023 burn-in MAE of the aggregate', round(burn_mae, 2), flush=True)

    singles = {name: experts_2024[name].to_frame(ev.MODEL) for name in EXPERTS}
    frame = predictions.assign(actual=data.consommation, rte_d1=data.prevision_j1)
    frame[RTE_STAR] = prior_year_hourly_reference(data.consommation, data.prevision_j1,
                                                 frame[ev.RTE])
    result = score_backtest(predictions, data.consommation, data.prevision_j1, frame[RTE_STAR])
    for name in ('EXP-007', 'EXP-005', 'EXP-004', 'EXP-008'):
        result[f'vs_{name}'] = paired_against_reference(predictions[ev.MODEL],
                                                       experts_2024[name], data)
    result['experts_seconds'] = seconds
    result['variant'] = VARIANT
    result['preregistration_commit'] = PREREGISTRATION_COMMIT
    result['burn_in_mae_2023'] = burn_mae
    result['expert_cache_sha256'] = hashlib.sha256(CACHE.read_bytes()).hexdigest()
    result['expert_backup_slots'] = backups

    cold = {d: bool(flag) for d, flag in labels.cold.items() if not pd.isna(flag)}
    label_missing = [str(d) for d, flag in labels.cold.items() if pd.isna(flag)]
    models = {'EXP-009': predictions, **singles}
    tables = {f'{name}_vs_rte': day_type_table(f[ev.MODEL], data.consommation,
                                               data.prevision_j1, cold)
              for name, f in models.items()}
    tables['EXP-009_vs_rte_star'] = day_type_table(predictions[ev.MODEL], data.consommation,
                                                   frame[RTE_STAR], cold)
    for name, table in tables.items():
        print(name)
        print(table.round(2).to_string())
    result['day_types'] = tables['EXP-009_vs_rte'].to_dict(orient='index')
    result['day_types_rte_star'] = tables['EXP-009_vs_rte_star'].to_dict(orient='index')
    result['day_type_tables'] = {k: v.to_dict(orient='index') for k, v in tables.items()}
    result['cold'] = 'D9 SYNOP labels, 12 stations, 4.84 C (labels.synop), applied in this script'
    result['n_label_days'] = len(labels)
    result['n_cold_days'] = sum(cold.values())
    result['label_missing_days'] = label_missing

    monthly = {name: monthly_diagnostic(pd.DataFrame({
        ev.MODEL: f[ev.MODEL], ev.ACTUAL: data.consommation, ev.RTE: data.prevision_j1}))
        for name, f in models.items()}
    table = pd.concat(monthly, axis=1)
    print(table.to_string())
    result['monthly'] = json.loads(table.to_json())

    both = pd.DataFrame({name: f[ev.MODEL] for name, f in models.items()}
                        | {ev.ACTUAL: data.consommation, ev.RTE: data.prevision_j1}).dropna()
    errors = both[list(models)].sub(both[ev.ACTUAL], axis=0)
    local = ev.local_dates(both.index)
    daily_signed = errors.groupby(local).mean()
    daily_abs = errors.abs().groupby(local).mean()
    expert_names = list(EXPERTS)
    result['daily_error_correlation_signed'] = daily_signed[expert_names].corr().round(3).to_dict()
    result['daily_error_correlation_abs'] = daily_abs[expert_names].corr().round(3).to_dict()
    print('daily signed error correlation')
    print(daily_signed[expert_names].corr().round(3).to_string())
    cold_days = [d for d in daily_abs.index if cold.get(d, False)]
    result['cold_days_descriptive'] = {
        'n_days': len(cold_days),
        **{f'mean_daily_mae_{name}': float(daily_abs.loc[cold_days, name].mean())
           for name in models}}
    print('cold days, mean daily MAE', result['cold_days_descriptive'])

    weights = aggregation.weights
    days_2024 = sorted({d for d in weights.index.get_level_values('day') if d >= FIRST})
    by_month = {m: mean_weights(weights, [d for d in days_2024 if d.month == m])
                for m in range(1, 13)}
    result['weights_by_month_2024'] = by_month
    result['weights_cold_days_2024'] = mean_weights(weights, [d for d in days_2024
                                                              if cold.get(d, False)])
    result['weights_non_cold_days_2024'] = mean_weights(weights, [d for d in days_2024
                                                                  if not cold.get(d, True)])
    result['weights_all_2024'] = mean_weights(weights, days_2024)
    result['weights_first_day_2024'] = mean_weights(weights, [FIRST])
    print(pd.DataFrame(by_month).T.to_string())
    print('cold days', result['weights_cold_days_2024'], '| all', result['weights_all_2024'])

    keys = ('model', 'rte', RTE_STAR, 'bootstrap_7', 'bootstrap_14', 'rte_star_bootstrap_7',
            'rte_star_bootstrap_14', 'vs_EXP-007', 'vs_EXP-005')
    if '--dry' in sys.argv:
        print(json.dumps({k: result[k] for k in keys}, indent=2, default=str))
        return
    result['mlflow_run_id'] = log_backtest(
        result, FIRST, LAST, exp='EXP-009', frame=frame,
        cold=cold, reference=experts_2024['EXP-007'], reference_name='EXP-007', params={
            'model': 'ML-Poly (Gaillard, Stoltz, van Erven 2014) gradient trick, absolute loss, '
                     'over EXP-004, EXP-005, EXP-007 and EXP-008',
            'variant': VARIANT, 'preregistration_commit': PREREGISTRATION_COMMIT,
            'update_rule': 'weights for day D use actuals of full local days up to D-2 only',
            'burn_in': '2023 out-of-sample expert forecasts, weights uniform on 2023-01-01, no reset',
            'learning_rate': 'none (eta_k = 1/(1+sum r_k^2))', 'tuning': 'none; variant chosen on 2023',
            'training_start': '2021-03-23', 'refit_of_experts': 'weekly, Monday cutoff',
            'seed': 'lightgbm seed 20240, others deterministic', 'bootstrap_seed': ev.BOOTSTRAP_SEED,
            'reference': 'EXP-007',
            'source_dataset': ','.join(f'{n}:{c}' for n, c in sources)
                              + ';gfs:raw.gfs_days,raw.gfs_points TMP',
            'cold_labels': 'D9 SYNOP, 12 stations, 4.84 C, labels.synop',
            'rte_star': 'a2: prior full Paris calendar year mean error by local hour'})
    stamp = int(time.time() * 1000)
    metrics = {'burn_in_mae_2023': burn_mae, 'experts_seconds': seconds,
               'n_label_missing_days': len(label_missing)}
    for ref in ('EXP-007', 'EXP-005', 'EXP-004', 'EXP-008'):
        key = ref.replace('-', '').lower()
        metrics[f'vs_{key}_n_slots'] = result[f'vs_{ref}']['n_slots']
        metrics[f'vs_{key}_reference_mae'] = result[f'vs_{ref}']['reference_mae']
        for block in (7, 14):
            for stat, value in result[f'vs_{ref}'][f'bootstrap_{block}'].items():
                if stat != 'verdict':
                    metrics[f'vs_{key}_bootstrap_{block}_{stat}'] = value
    for name, label in (('EXP-009_vs_rte', 'rte_d1'), ('EXP-009_vs_rte_star', 'rte_star')):
        for kind in ('ordinary', 'cold', 'cold_any', 'holiday', 'bridge', 'dst'):
            if kind in tables[name].index:
                row = tables[name].loc[kind]
                metrics[f'lab_{kind}_model_vs_{label}'] = row[ev.MODEL]
                metrics[f'lab_{kind}_{label}'] = row[ev.RTE]
                metrics[f'lab_{kind}_n_days'] = row['n_days']
    for expert, value in result['weights_all_2024'].items():
        metrics[f'mean_weight_{expert.replace("-", "").lower()}'] = value
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
    experts_2024.to_csv(OUTPUT / 'experts_2024.csv', index_label='value_time_utc')
    weights.to_csv(OUTPUT / 'weights.csv')
    (OUTPUT / 'results.json').write_text(json.dumps(result, indent=2, default=str),
                                         encoding='utf-8')
    print(json.dumps({k: result[k] for k in (*keys, 'mlflow_run_id', 'mlflow_status')},
                     indent=2, default=str))


if __name__ == '__main__':
    main()
