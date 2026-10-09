"""EXP-011: adaptive conformal 80% intervals around the EXP-009 aggregate, validation year 2024.

One change on EXP-010: the quantile level of each local slot is adapted online (Gibbs and Candes
2021) with the step chosen on 2023 and committed in EXP-011.md. There is no dry mode: the script
is run once on 2024 after the choice commit.
"""

import hashlib
import json
import subprocess
import time
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from expert_forecasts import EXPERTS, expert_forecasts
from run_exp002 import load
from run_exp003 import SOURCE_QUERY, day_type_table
from run_exp009 import EXPECTED
from run_exp010 import EXP009_MAE, EXP009_PREDICTIONS, WINDOW_DAYS
from select_aggregation import CACHE

from load_forecast import evaluation as ev
from load_forecast.aggregation import aggregate
from load_forecast.backtest import log_backtest, score_backtest
from load_forecast.baseline import FIRST, LAST
from load_forecast.conformal import (
    adaptive_conformal_intervals,
    conformal_intervals,
    coverage_bootstrap,
    coverage_difference_bootstrap,
    interval_report,
    scored_slots,
)
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.experiment_reports import committed_report
from load_forecast.labels import read_labels
from load_forecast.rte_reference import RTE_STAR, prior_year_hourly_reference
from load_forecast.tracking import mlflow_tracking_uri

OUTPUT = Path('data/experiments/EXP-011')
EXP010_INTERVALS = Path('data/experiments/EXP-010/intervals.csv')
# Chosen on 2023 only by scripts/select_aci_step.py and committed in EXP-011.md before this run.
GAMMA = 0.001
PREREGISTRATION_COMMIT = '3d816764b94bd8a88037fc2b0b7730e66fad1e64'


def check_preregistration() -> None:
    """Refuse to run unless the 2023 choice is in the history and names the step used here."""
    ancestor = subprocess.run(['git', 'merge-base', '--is-ancestor', PREREGISTRATION_COMMIT, 'HEAD'],
                              check=False)
    if ancestor.returncode != 0:
        raise RuntimeError('The pre-registration commit is not in the history of HEAD')
    text = committed_report(PREREGISTRATION_COMMIT, 'EXP-011.md')
    if f'the chosen step is **gamma={GAMMA}**' not in text:
        raise RuntimeError('The committed EXP-011.md does not record this step')


def block_report(scored: pd.DataFrame, cold: dict) -> dict:
    """Interval report with the 14-day check, months and day-level diagnostics."""
    report = interval_report(scored, cold)
    report['check_14'] = coverage_bootstrap(scored, block_days=ev.CHECK_BLOCK_DAYS)
    dates = pd.DatetimeIndex(ev.local_dates(scored.index))
    report['by_month'] = {
        int(m): {'coverage': float(part.covered.mean()), 'mean_width_mw': float(part.width.mean()),
                 'n_slots': int(len(part))}
        for m, part in scored.groupby(dates.month)}
    daily_share = scored.covered.groupby(dates).mean()
    report['share_days_all_slots_covered'] = float((daily_share == 1.0).mean())
    report['daily_coverage_quartiles'] = daily_share.quantile([0.25, 0.5, 0.75]).round(4).to_dict()
    below = scored.actual < scored.lower
    report['miss_below_share'] = float(below.sum() / (~scored.covered).sum())
    return report


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
        print(name, 'reproduced, MAE', round(mae, 2), flush=True)

    experts = pd.concat([burn_in, experts_2024])
    if experts.index.has_duplicates or not experts.index.is_monotonic_increasing:
        raise RuntimeError('2023 and 2024 expert forecasts overlap or are unordered')
    point_all = aggregate(experts, data.consommation, per_slot=False).forecast
    in_2024 = experts.index >= start
    predictions = point_all.loc[in_2024].to_frame()
    saved = pd.read_csv(EXP009_PREDICTIONS, index_col=0, parse_dates=True)[ev.MODEL]
    if not (saved.index.equals(predictions.index) and (saved - predictions[ev.MODEL]).abs().max() < 1e-6):
        raise RuntimeError('The EXP-009 point forecast was not reproduced slot by slot')
    frame = predictions.assign(actual=data.consommation, rte_d1=data.prevision_j1)
    frame[RTE_STAR] = prior_year_hourly_reference(data.consommation, data.prevision_j1,
                                                 frame[ev.RTE])
    result = score_backtest(predictions, data.consommation, data.prevision_j1, frame[RTE_STAR])
    if abs(result['model']['mae'] - EXP009_MAE) > 0.01:
        raise RuntimeError(f'EXP-009 MAE not reproduced: {result["model"]["mae"]:.2f}')
    print('EXP-009 point forecast reproduced, MAE', round(result['model']['mae'], 2), flush=True)

    # Both interval families need the 2023 residuals, so they are built on the full 2023 + 2024 series.
    static = conformal_intervals(point_all, data.consommation, window_days=WINDOW_DAYS)
    adaptive = adaptive_conformal_intervals(point_all, data.consommation, window_days=WINDOW_DAYS,
                                            gamma=GAMMA)
    zero = adaptive_conformal_intervals(point_all, data.consommation, window_days=WINDOW_DAYS,
                                        gamma=0.0)
    if not np.array_equal(zero[['lower', 'upper']].to_numpy(), static.to_numpy(), equal_nan=True):
        raise RuntimeError('gamma = 0 does not reproduce the static intervals')
    static, adaptive = static.loc[in_2024], adaptive.loc[in_2024]
    saved_010 = pd.read_csv(EXP010_INTERVALS, index_col=0, parse_dates=True)
    mine_010 = scored_slots(static, predictions[ev.MODEL], data.consommation)
    if not (saved_010.index.equals(mine_010.index)
            and (saved_010[['lower', 'upper']] - mine_010[['lower', 'upper']]).abs().max().max() < 1e-6):
        raise RuntimeError('The EXP-010 intervals were not reproduced slot by slot')
    print('EXP-010 intervals reproduced slot by slot', flush=True)

    cold = {d: bool(flag) for d, flag in labels.cold.items() if not pd.isna(flag)}
    result['label_missing_days'] = [str(d) for d, flag in labels.cold.items() if pd.isna(flag)]
    for name in (static, adaptive):
        undefined = int(name.isna().any(axis=1).sum())
        if undefined:
            raise RuntimeError(f'{undefined} 2024 slots without a defined interval')
    scored_new = scored_slots(adaptive[['lower', 'upper']], predictions[ev.MODEL], data.consommation)
    scored_new['alpha_t'] = adaptive.alpha_t
    scored_old = mine_010
    report = {'exp011': block_report(scored_new, cold), 'exp010': block_report(scored_old, cold)}
    report['coverage_difference_vs_exp010'] = coverage_difference_bootstrap(scored_new, scored_old)
    report['coverage_difference_vs_exp010_14'] = coverage_difference_bootstrap(
        scored_new, scored_old, block_days=ev.CHECK_BLOCK_DAYS)
    dates = pd.DatetimeIndex(ev.local_dates(scored_new.index))
    report['mean_alpha_t_by_month'] = {int(m): float(v)
                                       for m, v in scored_new.alpha_t.groupby(dates.month).mean().items()}
    report['alpha_t_range'] = [float(scored_new.alpha_t.min()), float(scored_new.alpha_t.max())]
    report['n_slots_without_actual'] = int(len(predictions) - len(scored_new))
    result['intervals'] = report
    result['gamma'] = GAMMA
    result['window_days'] = WINDOW_DAYS
    result['preregistration_commit'] = PREREGISTRATION_COMMIT
    result['expert_cache_sha256'] = hashlib.sha256(CACHE.read_bytes()).hexdigest()
    result['experts_seconds'] = seconds
    result['expert_backup_slots'] = backups
    result['n_label_days'] = len(labels)
    result['n_cold_days'] = sum(cold.values())
    result['cold'] = 'D9 SYNOP labels, 4.84 C'
    result['day_types'] = day_type_table(predictions[ev.MODEL], data.consommation, data.prevision_j1,
                                         cold).to_dict(orient='index')
    result['day_types_rte_star'] = day_type_table(predictions[ev.MODEL], data.consommation,
                                                  frame[RTE_STAR], cold).to_dict(orient='index')
    print(pd.DataFrame(result['day_types']).T.round(2).to_string())
    print(pd.DataFrame(result['day_types_rte_star']).T.round(2).to_string())
    for key in ('exp010', 'exp011'):
        print(key, json.dumps({k: v for k, v in report[key].items() if k != 'by_month'}, indent=2,
                              default=str))
        print(pd.DataFrame(report[key]['by_month']).T.round(4).to_string())
    print(json.dumps({k: report[k] for k in ('coverage_difference_vs_exp010',
                                              'coverage_difference_vs_exp010_14',
                                              'mean_alpha_t_by_month', 'alpha_t_range')}, indent=2))
    # Files first, so a later failure of the tracking server cannot lose the numbers.
    OUTPUT.mkdir(parents=True, exist_ok=True)
    scored_new.to_csv(OUTPUT / 'intervals.csv', index_label='value_time_utc')
    experts_2024.to_csv(OUTPUT / 'experts_2024.csv', index_label='value_time_utc')
    (OUTPUT / 'results.json').write_text(json.dumps(result, indent=2, default=str), encoding='utf-8')

    new = report['exp011']
    result['mlflow_run_id'] = log_backtest(
        result, FIRST, LAST, exp='EXP-011', frame=frame,
        cold=cold, params={
            'model': 'EXP-009 ML-Poly aggregate, point forecast unchanged',
            'interval': 'adaptive conformal inference (Gibbs and Candes 2021) on rolling split-conformal '
                        '80 percent per local slot, tails alpha_t / 2, ranks floor((n+1)*a/2) and '
                        'ceil((n+1)*(1-a/2)) clipped to 1..n',
            'window_days': WINDOW_DAYS, 'gamma': GAMMA, 'alpha_start': 0.2,
            'preregistration_commit': PREREGISTRATION_COMMIT,
            'update_rule': 'state and window absorb full local days up to D-2 only, one update per day',
            'burn_in': '2023 out-of-sample residuals of the EXP-009 aggregate',
            'tuning': 'gamma chosen on 2023 H2 from {0.001, 0.005, 0.01, 0.05}, no 2024 dry run',
            'training_start': '2021-03-23', 'bootstrap_seed': ev.BOOTSTRAP_SEED,
            'reference': 'EXP-010',
            'source_dataset': ','.join(f'{n}:{c}' for n, c in sources)
                              + ';gfs:raw.gfs_days,raw.gfs_points TMP',
            'cold_labels': 'D9 SYNOP, 12 stations, 4.84 C, labels.synop'})
    stamp = int(time.time() * 1000)
    metrics = {'interval_coverage': new['coverage'], 'interval_coverage_low': new['low'],
               'interval_coverage_high': new['high'],
               'interval_coverage_14_low': new['check_14']['low'],
               'interval_coverage_14_high': new['check_14']['high'],
               'interval_mean_width_mw': new['mean_width_mw'],
               'pinball_0_1': new['pinball']['0.1'], 'pinball_0_5': new['pinball']['0.5'],
               'pinball_0_9': new['pinball']['0.9'], 'window_days': WINDOW_DAYS, 'gamma': GAMMA,
               'interval_n_slots': new['n_slots'],
               'exp010_interval_coverage': report['exp010']['coverage'],
               'exp010_interval_mean_width_mw': report['exp010']['mean_width_mw'],
               'coverage_difference_vs_exp010': report['coverage_difference_vs_exp010']['difference']}
    for season, row in new['by_season'].items():
        metrics[f'coverage_{season}'] = row['coverage']
    for kind, row in new['by_day_type'].items():
        metrics[f'coverage_{kind}'] = row['coverage']
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
    (OUTPUT / 'results.json').write_text(json.dumps(result, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'coverage': new['coverage'], 'low': new['low'], 'high': new['high'],
                      'mlflow_run_id': result['mlflow_run_id'],
                      'mlflow_status': result['mlflow_status']}, indent=2))


if __name__ == '__main__':
    main()
