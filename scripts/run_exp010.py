"""EXP-010: rolling split-conformal 80% intervals around the EXP-009 aggregate, validation year 2024."""

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
from run_exp002 import load
from run_exp003 import SOURCE_QUERY, day_type_table
from run_exp009 import EXPECTED
from select_aggregation import CACHE

from load_forecast import evaluation as ev
from load_forecast.aggregation import aggregate
from load_forecast.backtest import log_backtest, score_backtest
from load_forecast.baseline import FIRST, LAST
from load_forecast.conformal import (
    conformal_intervals,
    coverage_bootstrap,
    interval_report,
    scored_slots,
)
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.experiment_reports import committed_report
from load_forecast.labels import read_labels
from load_forecast.rte_reference import RTE_STAR, prior_year_hourly_reference
from load_forecast.tracking import mlflow_tracking_uri

OUTPUT = Path('data/experiments/EXP-010')
EXP009_PREDICTIONS = Path('data/experiments/EXP-009/backtest_predictions.csv')
EXP009_EXPERTS = Path('data/experiments/EXP-009/experts_2024.csv')
EXP009_MAE = 828.52
# Chosen on 2023 only by scripts/select_conformal_window.py and committed in EXP-010.md before this run.
WINDOW_DAYS = 182
PREREGISTRATION_COMMIT = '312bae1a6bf3ea3853002172b158c33467ea828d'


def check_preregistration() -> None:
    """Refuse to run unless the 2023 choice is in the history and names the window used here."""
    ancestor = subprocess.run(['git', 'merge-base', '--is-ancestor', PREREGISTRATION_COMMIT, 'HEAD'],
                              check=False)
    if ancestor.returncode != 0:
        raise RuntimeError('The pre-registration commit is not in the history of HEAD')
    text = committed_report(PREREGISTRATION_COMMIT, 'EXP-010.md')
    if f'the chosen window is **N={WINDOW_DAYS}**' not in text:
        raise RuntimeError('The committed EXP-010.md does not record this window')


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
    if '--cached' in sys.argv:
        if '--dry' not in sys.argv:
            raise RuntimeError('--cached skips the expert recomputation: dry runs only')
        experts_2024 = pd.read_csv(EXP009_EXPERTS, index_col=0, parse_dates=True)[list(EXPERTS)]
        backups = {}
    else:
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

    # Intervals need the 2023 residuals, so they are built on the full 2023 + 2024 series.
    bounds = conformal_intervals(point_all, data.consommation, window_days=WINDOW_DAYS)
    bounds = bounds.loc[in_2024]
    cold = {d: bool(flag) for d, flag in labels.cold.items() if not pd.isna(flag)}
    label_missing = [str(d) for d, flag in labels.cold.items() if pd.isna(flag)]
    scored = scored_slots(bounds, predictions[ev.MODEL], data.consommation)
    n_undefined = int(bounds.isna().any(axis=1).sum())
    if n_undefined:
        raise RuntimeError(f'{n_undefined} 2024 slots without a defined interval')
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
    report['n_slots_without_actual'] = int(len(predictions) - len(scored))
    below = scored.actual < scored.lower
    report['miss_below_share'] = float(below.sum() / (~scored.covered).sum())
    result['intervals'] = report
    result['window_days'] = WINDOW_DAYS
    result['preregistration_commit'] = PREREGISTRATION_COMMIT
    result['expert_cache_sha256'] = hashlib.sha256(CACHE.read_bytes()).hexdigest()
    result['experts_seconds'] = seconds
    result['expert_backup_slots'] = backups
    result['n_label_days'] = len(labels)
    result['n_cold_days'] = sum(cold.values())
    result['label_missing_days'] = label_missing
    table = day_type_table(predictions[ev.MODEL], data.consommation, data.prevision_j1, cold)
    result['day_types'] = table.to_dict(orient='index')
    print(table.round(2).to_string())
    print(json.dumps({k: v for k, v in report.items() if k != 'by_month'}, indent=2, default=str))
    print(pd.DataFrame(report['by_month']).T.round(4).to_string())
    if '--dry' in sys.argv:
        return

    result['mlflow_run_id'] = log_backtest(
        result, FIRST, LAST, exp='EXP-010', frame=frame,
        cold=cold, params={
            'model': 'EXP-009 ML-Poly aggregate, point forecast unchanged',
            'interval': 'rolling split-conformal 80 percent per local slot, signed residual order '
                        'statistics floor((n+1)*0.1) and ceil((n+1)*0.9)',
            'window_days': WINDOW_DAYS, 'preregistration_commit': PREREGISTRATION_COMMIT,
            'update_rule': 'calibration residuals of full local days up to D-2 only',
            'burn_in': '2023 out-of-sample residuals of the EXP-009 aggregate',
            'tuning': 'window chosen on 2023 H2 from {28, 56, 91, 182}, 2024 untouched',
            'training_start': '2021-03-23', 'bootstrap_seed': ev.BOOTSTRAP_SEED,
            'reference': 'EXP-009',
            'source_dataset': ','.join(f'{n}:{c}' for n, c in sources)
                              + ';gfs:raw.gfs_days,raw.gfs_points TMP',
            'cold_labels': 'D9 SYNOP, 12 stations, 4.84 C, labels.synop'})
    stamp = int(time.time() * 1000)
    metrics = {'interval_coverage': report['coverage'], 'interval_coverage_low': report['low'],
               'interval_coverage_high': report['high'],
               'interval_coverage_14_low': report['check_14']['low'],
               'interval_coverage_14_high': report['check_14']['high'],
               'interval_mean_width_mw': report['mean_width_mw'],
               'pinball_0_1': report['pinball']['0.1'], 'pinball_0_5': report['pinball']['0.5'],
               'pinball_0_9': report['pinball']['0.9'], 'window_days': WINDOW_DAYS,
               'interval_n_slots': report['n_slots']}
    for season, row in report['by_season'].items():
        metrics[f'coverage_{season}'] = row['coverage']
    for kind, row in report['by_day_type'].items():
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
    OUTPUT.mkdir(parents=True, exist_ok=True)
    scored.to_csv(OUTPUT / 'intervals.csv', index_label='value_time_utc')
    experts_2024.to_csv(OUTPUT / 'experts_2024.csv', index_label='value_time_utc')
    (OUTPUT / 'results.json').write_text(json.dumps(result, indent=2, default=str),
                                         encoding='utf-8')
    print(json.dumps({'coverage': report['coverage'], 'low': report['low'], 'high': report['high'],
                      'mlflow_run_id': result['mlflow_run_id'],
                      'mlflow_status': result['mlflow_status']}, indent=2))


if __name__ == '__main__':
    main()
