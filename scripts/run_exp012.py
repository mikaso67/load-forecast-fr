"""EXP-012: seasonal group conformal 80% intervals around the EXP-009 aggregate, validation year 2024.

One change on EXP-010: the calibration pool of a slot is the residuals of the same season within
the last 365 days (pre-registered in EXP-012.md, nothing tuned). There is no dry mode: the script is
run once on 2024 after the pre-registration commit. EXP-010 is reproduced in the same run, and a
365-day pool without season is reported as a descriptive control.
"""

import hashlib
import json
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from expert_forecasts import EXPERTS, expert_forecasts
from run_exp002 import load
from run_exp003 import SOURCE_QUERY, day_type_table
from run_exp009 import EXPECTED
from run_exp010 import EXP009_MAE, EXP009_PREDICTIONS, WINDOW_DAYS
from run_exp011 import block_report
from select_aggregation import CACHE

from load_forecast import evaluation as ev
from load_forecast.aggregation import aggregate
from load_forecast.backtest import log_backtest, score_backtest
from load_forecast.baseline import FIRST, LAST
from load_forecast.conformal import (
    conformal_intervals,
    coverage_bootstrap,
    coverage_difference_bootstrap,
    scored_slots,
    seasonal_conformal_intervals,
)
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.experiment_reports import committed_report
from load_forecast.labels import read_labels
from load_forecast.rte_reference import RTE_STAR, prior_year_hourly_reference
from load_forecast.tracking import mlflow_tracking_uri

OUTPUT = Path('data/experiments/EXP-012')
EXP010_INTERVALS = Path('data/experiments/EXP-010/intervals.csv')
EXP011_INTERVALS = Path('data/experiments/EXP-011/intervals.csv')
EXP011_RESULTS = Path('data/experiments/EXP-011/results.json')
SEASON_WINDOW_DAYS = 365
SEASONS = ('winter', 'spring', 'summer', 'autumn')
PREREGISTRATION_COMMIT = '88a6c1083022e97653b96a29659127f0d2a2fb3d'
PREREGISTRATION_TITLE = '# Pre-registration (written and committed before any seasonal interval was computed'


def check_preregistration() -> None:
    """Refuse to run unless the pre-registration is in the history and holds no result."""
    ancestor = subprocess.run(['git', 'merge-base', '--is-ancestor', PREREGISTRATION_COMMIT, 'HEAD'],
                              check=False)
    if ancestor.returncode != 0:
        raise RuntimeError('The pre-registration commit is not in the history of HEAD')
    text = committed_report(PREREGISTRATION_COMMIT, 'EXP-012.md')
    if PREREGISTRATION_TITLE not in text or '# Results' in text:
        raise RuntimeError('The committed EXP-012.md is not a results-free pre-registration')


def season_of(index: pd.DatetimeIndex) -> pd.Series:
    return pd.Series([ev.get_season(d.month) for d in ev.local_dates(index)], index=index)


def seasonal_distance(report: dict) -> float:
    """Mean absolute distance of the four seasonal coverages to 80 percent, in points."""
    return float(np.mean([abs(report['by_season'][s]['coverage'] - 0.8) for s in SEASONS]) * 100)


def pool_statistics(pool: pd.DataFrame) -> dict:
    """Pool sizes of the scored 2024 slots overall, by season and by month, and January 2024."""
    dates = pd.DatetimeIndex(ev.local_dates(pool.index))
    seasons = season_of(pool.index)
    january = pool[dates.month == 1]
    jan_dates = pd.DatetimeIndex(ev.local_dates(january.index))
    from_2023 = {}
    for day in sorted(set(jan_dates.date)):
        last = day - timedelta(days=2)
        first = last - timedelta(days=SEASON_WINDOW_DAYS - 1)
        known = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        winter = [d for d in known if ev.get_season(d.month) == 'winter' and d >= date(2023, 1, 1)]
        from_2023[str(day)] = sum(d.year == 2023 for d in winter) / len(winter)
    return {
        'min': int(pool.n_pool.min()), 'mean': float(pool.n_pool.mean()),
        'by_season': {s: {'min': int(g.n_pool.min()), 'mean': float(g.n_pool.mean())}
                      for s, g in pool.groupby(seasons)},
        'by_month': {int(m): {'min': int(g.n_pool.min()), 'mean': float(g.n_pool.mean())}
                     for m, g in pool.groupby(dates.month)},
        'january': {'min': int(january.n_pool.min()), 'max': int(january.n_pool.max()),
                    'share_from_2023_first_day': from_2023['2024-01-01'],
                    'share_from_2023_last_day': from_2023['2024-01-31'],
                    'share_from_2023_by_day': from_2023},
    }


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

    # All families need the 2023 residuals, so they are built on the full 2023 + 2024 series.
    static = conformal_intervals(point_all, data.consommation, window_days=WINDOW_DAYS).loc[in_2024]
    control = conformal_intervals(point_all, data.consommation,
                                  window_days=SEASON_WINDOW_DAYS).loc[in_2024]
    seasonal = seasonal_conformal_intervals(point_all, data.consommation,
                                            window_days=SEASON_WINDOW_DAYS).loc[in_2024]
    saved_010 = pd.read_csv(EXP010_INTERVALS, index_col=0, parse_dates=True)
    scored_010 = scored_slots(static, predictions[ev.MODEL], data.consommation)
    if not (saved_010.index.equals(scored_010.index)
            and (saved_010[['lower', 'upper']] - scored_010[['lower', 'upper']]).abs().max().max() < 1e-6):
        raise RuntimeError('The EXP-010 intervals were not reproduced slot by slot')
    print('EXP-010 intervals reproduced slot by slot', flush=True)

    scored_011 = pd.read_csv(EXP011_INTERVALS, index_col=0, parse_dates=True)
    stored_011 = json.loads(EXP011_RESULTS.read_text(encoding='utf-8'))['intervals']['exp011']
    if abs(float(scored_011.covered.mean()) - stored_011['coverage']) > 1e-9:
        raise RuntimeError('The saved EXP-011 intervals do not match its stored coverage')

    cold = {d: bool(flag) for d, flag in labels.cold.items() if not pd.isna(flag)}
    result['label_missing_days'] = [str(d) for d, flag in labels.cold.items() if pd.isna(flag)]
    scored_012 = scored_slots(seasonal[['lower', 'upper']], predictions[ev.MODEL], data.consommation)
    scored_ctl = scored_slots(control, predictions[ev.MODEL], data.consommation)
    undefined = {'exp010': int(static.isna().any(axis=1).sum()),
                 'control365': int(control.isna().any(axis=1).sum()),
                 'exp012': int(seasonal[['lower', 'upper']].isna().any(axis=1).sum())}
    print('slots without a defined interval', undefined, flush=True)
    families = {'exp012': scored_012, 'exp010': scored_010, 'exp011': scored_011,
                'control365': scored_ctl}
    report = {name: block_report(scored, cold) for name, scored in families.items()}
    for name, scored in families.items():
        masks = season_of(scored.index)
        report[name]['by_season_bootstrap'] = {
            s: {k: v for k, v in coverage_bootstrap(scored[masks == s]).items()
                if k in ('coverage', 'low', 'high', 'n_slots')} for s in SEASONS}
        report[name]['seasonal_distance_points'] = seasonal_distance(report[name])
    for other in ('exp010', 'exp011', 'control365'):
        for days, suffix in ((ev.BLOCK_DAYS, ''), (ev.CHECK_BLOCK_DAYS, '_14')):
            report[f'coverage_difference_vs_{other}{suffix}'] = coverage_difference_bootstrap(
                scored_012, families[other], block_days=days)
        mine, theirs = season_of(scored_012.index), season_of(families[other].index)
        report[f'coverage_difference_by_season_vs_{other}'] = {
            s: coverage_difference_bootstrap(scored_012[mine == s], families[other][theirs == s])
            for s in SEASONS}
    report['pool'] = pool_statistics(seasonal)
    report['n_slots_without_actual'] = int(len(predictions) - len(scored_012))
    report['undefined_slots'] = undefined
    result['intervals'] = report
    result['window_days'] = SEASON_WINDOW_DAYS
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
    for key in families:
        print(key, json.dumps({k: v for k, v in report[key].items() if k != 'by_month'}, indent=2,
                              default=str))
        print(pd.DataFrame(report[key]['by_month']).T.round(4).to_string())
    print(json.dumps({k: v for k, v in report.items() if k not in families}, indent=2, default=str))
    # Files first, so a later failure of the tracking server cannot lose the numbers.
    OUTPUT.mkdir(parents=True, exist_ok=True)
    scored_012.assign(n_pool=seasonal.n_pool).to_csv(OUTPUT / 'intervals.csv', index_label='value_time_utc')
    scored_ctl.to_csv(OUTPUT / 'intervals_control365.csv', index_label='value_time_utc')
    experts_2024.to_csv(OUTPUT / 'experts_2024.csv', index_label='value_time_utc')
    (OUTPUT / 'results.json').write_text(json.dumps(result, indent=2, default=str), encoding='utf-8')

    new = report['exp012']
    result['mlflow_run_id'] = log_backtest(
        result, FIRST, LAST, exp='EXP-012', frame=frame,
        cold=cold, params={
            'model': 'EXP-009 ML-Poly aggregate, point forecast unchanged',
            'interval': 'seasonal group (Mondrian) split conformal 80 percent per local slot, pool = same '
                        'slot, same Paris-local season as D, days D-2-364 to D-2, ranks '
                        'floor((n+1)*0.1) and ceil((n+1)*0.9)',
            'window_days': SEASON_WINDOW_DAYS,
            'preregistration_commit': PREREGISTRATION_COMMIT,
            'update_rule': 'pool absorbs full local days up to D-2 only',
            'burn_in': '2023 out-of-sample residuals of the EXP-009 aggregate',
            'tuning': 'none, no 2023 choice step and no 2024 dry run',
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
               'pinball_0_9': new['pinball']['0.9'], 'window_days': SEASON_WINDOW_DAYS,
               'interval_n_slots': new['n_slots'],
               'seasonal_distance_points': new['seasonal_distance_points'],
               'pool_min': report['pool']['min'],
               'exp010_interval_coverage': report['exp010']['coverage'],
               'exp010_interval_mean_width_mw': report['exp010']['mean_width_mw'],
               'exp010_seasonal_distance_points': report['exp010']['seasonal_distance_points'],
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
