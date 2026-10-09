"""TEST-2025: the single final test of the two milestone candidates of decision D18.

Candidate 1: EXP-009 ML-Poly aggregate with EXP-012 seasonal conformal 80% intervals.
Candidate 2: EXP-007 GAM plus Kalman, point forecast only.
Everything is frozen as chosen on 2023 and committed before; the protocol is
`docs/experiments/TEST-2025.md`.

    python scripts/run_test2025.py --rehearsal   # same analysis on 2024, saved experts, no 2025, no MLflow
    python scripts/run_test2025.py               # the one real run, refused unless the protocol is committed

The real run refuses to start when the working tree has uncommitted changes in tracked files, when the
committed protocol is missing or holds results, or when a result file or an MLflow run of the test
already exists. Nothing after 2025-12-31 is read.
"""

import argparse
import hashlib
import json
import subprocess
import time
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from expert_forecasts import EXPERTS, expert_forecasts
from run_exp002 import TRAINING_START
from run_exp003 import SOURCE_QUERY, day_type_table, monthly_diagnostic
from run_exp009 import EXPECTED
from run_exp011 import block_report
from select_aggregation import CACHE

from load_forecast import evaluation as ev
from load_forecast.aggregation import aggregate
from load_forecast.backtest import log_backtest, score_backtest
from load_forecast.conformal import (
    coverage_bootstrap,
    scored_slots,
    seasonal_conformal_intervals,
)
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.labels import read_labels
from load_forecast.rte_reference import RTE_STAR, prior_year_hourly_reference
from load_forecast.tracking import mlflow_tracking_uri

QUERY = """
SELECT value_time, consommation, prevision_j1
FROM {table}
WHERE value_time >= %(start)s AND value_time < %(end)s
ORDER BY value_time
"""
TEST_FIRST, TEST_LAST = date(2025, 1, 1), date(2025, 12, 31)
VALIDATION_FIRST, VALIDATION_LAST = date(2024, 1, 1), date(2024, 12, 31)
PROTOCOL = Path('docs/experiments/TEST-2025.md')
PROTOCOL_TITLE = '# Protocol (written and committed before any 2025 value was read'
OUTPUT = Path('data/experiments/TEST-2025')
EXP009_EXPERTS = Path('data/experiments/EXP-009/experts_2024.csv')
EXP009_PREDICTIONS = Path('data/experiments/EXP-009/backtest_predictions.csv')
EXP012_INTERVALS = Path('data/experiments/EXP-012/intervals.csv')
WINDOW_DAYS = 365
SEASONS = ('winter', 'spring', 'summer', 'autumn')
CANDIDATES = ('candidate1', 'candidate2')
# 2024 figures of the accepted experiments, reproduced in the rehearsal.
EXP009_MAE, EXP007_MAE, EXP012_COVERAGE = 828.52, 885.32, 0.801


def git(*args: str) -> str:
    return subprocess.run(['git', *args], check=True, capture_output=True, text=True,
                          encoding='utf-8').stdout


def mlflow_runs_of_test() -> list:
    reply = requests.post(f'{mlflow_tracking_uri()}/api/2.0/mlflow/runs/search', json={
        'experiment_ids': ['0'], 'filter': "tags.exp = 'TEST-2025'", 'max_results': 10}, timeout=30)
    reply.raise_for_status()
    return reply.json().get('runs', [])


def check_protocol() -> str:
    """Refuse to run unless the protocol is committed as it is, results-free, and no run exists."""
    if git('status', '--porcelain', '--untracked-files=no').strip():
        raise RuntimeError('Uncommitted changes in tracked files: commit them first')
    text = git('show', f'HEAD:{PROTOCOL.as_posix()}')
    if PROTOCOL_TITLE not in text or '# Results' in text:
        raise RuntimeError('The committed TEST-2025.md is not a results-free protocol')
    if (OUTPUT / 'results.json').exists():
        raise RuntimeError('TEST-2025 was already run: results.json exists. No rerun.')
    if mlflow_runs_of_test():
        raise RuntimeError('An MLflow run tagged exp=TEST-2025 already exists. No rerun.')
    return git('rev-parse', 'HEAD').strip()


def load_consumption(last: date) -> pd.DataFrame:
    start = pd.Timestamp(TRAINING_START, tz='Europe/Paris').tz_convert('UTC')
    end = pd.Timestamp(last + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
    with connect_database() as connection:
        cursor = connection.execute(clean_query(QUERY), {'start': start, 'end': end})
        data = pd.DataFrame(cursor.fetchall(), columns=[c.name for c in cursor.description])
    data.index = pd.DatetimeIndex(pd.to_datetime(data.pop('value_time'), utc=True))
    if data.index.max() >= end or data.index.has_duplicates:
        raise RuntimeError('Consumption read beyond the allowed period, or duplicated')
    return data


def season_of(index: pd.DatetimeIndex) -> pd.Series:
    return pd.Series([ev.get_season(d.month) for d in ev.local_dates(index)], index=index)


def bootstrap_pair(candidate: pd.Series, other: pd.Series, actual: pd.Series, rte: pd.Series,
                   first: date, last: date) -> dict:
    """Daily MAE difference candidate minus other on the same slots, 7-day and 14-day blocks."""
    frame = pd.DataFrame({ev.MODEL: candidate, ev.ACTUAL: actual, ev.RTE: rte}).dropna()
    mine = ev.daily_mae(frame)[ev.MODEL]
    theirs = ev.daily_mae(frame.assign(**{ev.MODEL: other.reindex(frame.index)}))[ev.MODEL]
    primary, check = ev.compare_with_check(mine, theirs, first, last)
    return {'n_slots': int(len(frame)), 'mae_candidate': float((frame[ev.ACTUAL] - frame[ev.MODEL]).abs().mean()),
            'mae_other': float((frame[ev.ACTUAL] - other.reindex(frame.index)).abs().mean()),
            'bootstrap_7': asdict(primary) | {'verdict': primary.verdict},
            'bootstrap_14': asdict(check) | {'verdict': check.verdict}}


def pool_statistics(intervals: pd.DataFrame) -> dict:
    dates = pd.DatetimeIndex(ev.local_dates(intervals.index))
    seasons = season_of(intervals.index)
    return {'min': int(intervals.n_pool.min()), 'mean': float(intervals.n_pool.mean()),
            'by_season': {s: {'min': int(g.n_pool.min()), 'mean': float(g.n_pool.mean())}
                          for s, g in intervals.groupby(seasons)},
            'by_month': {int(m): {'min': int(g.n_pool.min()), 'mean': float(g.n_pool.mean())}
                         for m, g in intervals.groupby(dates.month)}}


def analyse(experts: pd.DataFrame, data: pd.DataFrame, cold: dict, first: date, last: date,
            residuals_from: date) -> tuple[dict, dict]:
    """The whole analysis of one evaluation year from the complete expert table.

    `experts` holds every expert forecast from the 2023 burn-in to `last`. The aggregate is run on
    the full table, so its weights at `first` are the ones carried from the previous year.
    """
    aggregation = aggregate(experts, data.consommation, per_slot=False)
    start = pd.Timestamp(first, tz='Europe/Paris').tz_convert('UTC')
    end = pd.Timestamp(last + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
    in_year = (experts.index >= start) & (experts.index < end)
    point = aggregation.forecast.loc[in_year]
    expert_year = experts.loc[in_year]
    candidates = {'candidate1': point, 'candidate2': expert_year['EXP-007']}
    frame = pd.DataFrame({'actual': data.consommation.reindex(point.index),
                          'rte_d1': data.prevision_j1.reindex(point.index)})
    frame[RTE_STAR] = prior_year_hourly_reference(data.consommation, data.prevision_j1, frame.rte_d1)
    usable = pd.concat([frame, pd.DataFrame(candidates)], axis=1).notna().all(axis=1)
    common = frame.index[usable]
    result = {'n_slots_period': int(len(frame)), 'n_slots_common': int(len(common)),
              'excluded_slots': [t.isoformat() for t in frame.index.difference(common)][:20],
              'n_excluded': int(len(frame) - len(common))}
    actual, rte, star = frame.actual, frame.rte_d1, frame[RTE_STAR]
    for name, series in candidates.items():
        predictions = series.loc[common].to_frame(ev.MODEL)
        scored = score_backtest(predictions, actual, rte, star)
        scored['day_types_vs_rte'] = day_type_table(series.loc[common], actual, rte, cold).to_dict('index')
        scored['day_types_vs_rte_star'] = day_type_table(series.loc[common], actual, star,
                                                         cold).to_dict('index')
        scored.pop('cold')
        scored['mean_error_mw'] = float((series.loc[common] - actual.loc[common]).mean())
        result[name] = scored
    result['rte_mean_error_mw'] = float((rte.loc[common] - actual.loc[common]).mean())
    result['rte_star_mean_error_mw'] = float((star.loc[common] - actual.loc[common]).mean())
    result['candidate1_vs_candidate2'] = bootstrap_pair(candidates['candidate1'].loc[common],
                                                        candidates['candidate2'].loc[common],
                                                        actual, rte, first, last)
    monthly = pd.concat({'candidate1': monthly_diagnostic(pd.DataFrame({
        ev.MODEL: candidates['candidate1'], ev.ACTUAL: actual, ev.RTE: rte}).loc[common]),
        'candidate2': monthly_diagnostic(pd.DataFrame({
            ev.MODEL: candidates['candidate2'], ev.ACTUAL: actual, ev.RTE: rte}).loc[common]),
        'rte_star': monthly_diagnostic(pd.DataFrame({
            ev.MODEL: star, ev.ACTUAL: actual, ev.RTE: rte}).loc[common])}, axis=1)
    result['monthly'] = json.loads(monthly.to_json())
    result['expert_mae'] = {name: float((expert_year[name].loc[common] - actual.loc[common]).abs().mean())
                            for name in EXPERTS}
    days = sorted({d for d in aggregation.weights.index.get_level_values('day') if first <= d <= last})
    weights = aggregation.weights.loc[aggregation.weights.index.get_level_values('day').isin(days)]
    result['weights_by_month'] = {
        int(m): weights[[d.month == m for d in weights.index.get_level_values('day')]].mean()
        .round(4).to_dict() for m in range(1, 13)
        if any(d.month == m for d in weights.index.get_level_values('day'))}
    result['weights_first_day'] = weights.iloc[0].round(4).to_dict()
    cold_days = [d for d in days if cold.get(d, False)]
    result['weights_cold_days'] = weights.loc[weights.index.get_level_values('day').isin(
        cold_days)].mean().round(4).to_dict() if cold_days else {}

    residual_series = aggregation.forecast.loc[aggregation.forecast.index
                                               >= pd.Timestamp(residuals_from, tz='Europe/Paris')
                                               .tz_convert('UTC')]
    intervals = seasonal_conformal_intervals(residual_series, data.consommation,
                                             window_days=WINDOW_DAYS).loc[point.index]
    scored = scored_slots(intervals, point, data.consommation)
    report = block_report(scored, cold)
    masks = season_of(scored.index)
    report['by_season_bootstrap'] = {
        s: {k: v for k, v in coverage_bootstrap(scored[masks == s]).items()
            if k in ('coverage', 'low', 'high', 'n_slots')} for s in SEASONS}
    report['seasonal_distance_points'] = float(np.mean(
        [abs(report['by_season'][s]['coverage'] - 0.8) for s in SEASONS]) * 100)
    report['pool'] = pool_statistics(intervals)
    report['undefined_slots'] = int(intervals[['lower', 'upper']].isna().any(axis=1).sum())
    report['n_slots_without_actual'] = int(len(point) - len(scored))
    result['intervals'] = report
    result['n_cold_days'] = len(cold_days)
    result['n_cold_days_label'] = sum(cold.values())
    frame = frame.assign(**{ev.MODEL: point})
    return result, {'frame': frame, 'candidates': candidates, 'scored': scored,
                    'weights': aggregation.weights, 'common': common}


def print_summary(result: dict) -> None:
    for name in CANDIDATES:
        block = result[name]
        print(name, 'model', {k: round(v, 2) for k, v in block['model'].items()},
              '| raw RTE', {k: round(v, 2) for k, v in block['rte'].items()},
              '| RTE*', {k: round(v, 2) for k, v in block[RTE_STAR].items()})
        print('  vs raw RTE 7d', block['bootstrap_7'], '\n  vs RTE* 7d', block['rte_star_bootstrap_7'])
        print('  vs raw RTE 14d', block['bootstrap_14'], '\n  vs RTE* 14d', block['rte_star_bootstrap_14'])
        print(pd.DataFrame(block['day_types_vs_rte']).T.round(1).to_string())
        print(pd.DataFrame(block['day_types_vs_rte_star']).T.round(1).to_string())
    print('candidate1 vs candidate2', result['candidate1_vs_candidate2'])
    report = result['intervals']
    print({k: v for k, v in report.items() if k not in ('by_month', 'pool', 'daily_coverage_quartiles')})


def rehearsal() -> None:
    """The analysis on 2024 with the saved experts: it must reproduce EXP-009, EXP-007 and EXP-012."""
    burn_in = pd.read_csv(CACHE, index_col=0, parse_dates=True)[list(EXPERTS)]
    saved = pd.read_csv(EXP009_EXPERTS, index_col=0, parse_dates=True)[list(EXPERTS)]
    data = load_consumption(VALIDATION_LAST)
    with connect_database() as connection:
        labels = read_labels(connection, VALIDATION_FIRST, VALIDATION_LAST + timedelta(days=1))
    cold = {d: bool(flag) for d, flag in labels.cold.items() if not pd.isna(flag)}
    result, parts = analyse(pd.concat([burn_in, saved]), data, cold, VALIDATION_FIRST, VALIDATION_LAST,
                            residuals_from=date(2023, 1, 1))
    print_summary(result)
    got = {'candidate1': result['candidate1']['model']['mae'],
           'candidate2': result['candidate2']['model']['mae'],
           'coverage': result['intervals']['coverage']}
    print('rehearsal', got)
    if abs(got['candidate1'] - EXP009_MAE) > 0.01 or abs(got['candidate2'] - EXP007_MAE) > 0.01:
        raise RuntimeError('The rehearsal did not reproduce the 2024 point forecasts')
    if abs(got['coverage'] - EXP012_COVERAGE) > 0.002:
        raise RuntimeError('The rehearsal did not reproduce the EXP-012 coverage')
    point = pd.read_csv(EXP009_PREDICTIONS, index_col=0, parse_dates=True)[ev.MODEL]
    own = parts['candidates']['candidate1']
    if not (point.index.equals(own.index) and (point - own).abs().max() < 1e-6):
        raise RuntimeError('The EXP-009 point forecast was not reproduced slot by slot')
    saved_intervals = pd.read_csv(EXP012_INTERVALS, index_col=0, parse_dates=True)
    own_intervals = parts['scored']
    if not (saved_intervals.index.equals(own_intervals.index) and (
            saved_intervals[['lower', 'upper']] - own_intervals[['lower', 'upper']]).abs().max().max() < 1e-6):
        raise RuntimeError('The EXP-012 intervals were not reproduced slot by slot')
    print('rehearsal passed: EXP-009 and EXP-012 slot by slot, EXP-007 MAE')


def final_run() -> None:
    commit = check_protocol()
    if not CACHE.exists() or not EXP009_EXPERTS.exists():
        raise RuntimeError('The 2023 and 2024 expert forecasts of EXP-009 are missing')
    burn_in = pd.read_csv(CACHE, index_col=0, parse_dates=True)[list(EXPERTS)]
    saved_2024 = pd.read_csv(EXP009_EXPERTS, index_col=0, parse_dates=True)[list(EXPERTS)]
    data = load_consumption(TEST_LAST)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    began = time.perf_counter()
    # One continuous walk from 2024-01-01: the Kalman variance warm start and every refit follow the
    # production schedule, and the 2024 part must reproduce the accepted experts before 2025 is read.
    experts_all, backups = expert_forecasts(data.consommation, VALIDATION_FIRST, TEST_LAST,
                                            final_test=True)
    seconds = round(time.perf_counter() - began, 1)
    experts_all.to_csv(OUTPUT / 'experts_2024_2025.csv', index_label='value_time_utc')
    end_2024 = pd.Timestamp(VALIDATION_LAST + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
    part_2024 = experts_all.loc[experts_all.index < end_2024]
    difference = float((part_2024 - saved_2024.reindex(part_2024.index)).abs().max().max())
    for name in EXPERTS:
        frame = pd.DataFrame({ev.MODEL: part_2024[name], ev.ACTUAL: data.consommation,
                              ev.RTE: data.prevision_j1})
        mae = ev.score(frame).model.mae
        if abs(mae - EXPECTED[name]) > 0.01:
            raise RuntimeError(f'{name} did not reproduce on 2024: {mae:.2f} instead of {EXPECTED[name]}')
    if not saved_2024.index.equals(part_2024.index) or difference > 1e-3:
        raise RuntimeError(f'The 2024 experts differ from the saved ones by {difference} MW')
    experts = pd.concat([burn_in, experts_all])
    if experts.index.has_duplicates or not experts.index.is_monotonic_increasing:
        raise RuntimeError('Expert tables overlap or are unordered')
    saved_point = pd.read_csv(EXP009_PREDICTIONS, index_col=0, parse_dates=True)[ev.MODEL]
    aggregated = aggregate(experts.loc[experts.index < end_2024], data.consommation,
                           per_slot=False).forecast.loc[saved_point.index]
    if (saved_point - aggregated).abs().max() >= 1e-6:
        raise RuntimeError('The EXP-009 aggregate was not reproduced slot by slot on 2024')
    print('2024 reproduction passed (experts, aggregate); nothing from 2025 has been printed', flush=True)

    with connect_database() as connection:
        labels = read_labels(connection, TEST_FIRST, TEST_LAST + timedelta(days=1))
        start = pd.Timestamp(TEST_FIRST, tz='Europe/Paris').tz_convert('UTC')
        stop = pd.Timestamp(TEST_LAST + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
        sources = connection.execute(clean_query(SOURCE_QUERY), {'start': start, 'end': stop}).fetchall()
    cold = {d: bool(flag) for d, flag in labels.cold.items() if not pd.isna(flag)}
    result, parts = analyse(experts, data, cold, TEST_FIRST, TEST_LAST,
                            residuals_from=VALIDATION_FIRST)
    result['label_missing_days'] = [str(d) for d, flag in labels.cold.items() if pd.isna(flag)]
    result['n_label_days'] = len(labels)
    result['expert_backup_slots'] = backups
    result['experts_seconds'] = seconds
    result['reproduction_2024_max_abs_difference_mw'] = difference
    result['protocol_commit'] = commit
    result['expert_cache_sha256'] = hashlib.sha256(CACHE.read_bytes()).hexdigest()
    result['experts_2024_sha256'] = hashlib.sha256(EXP009_EXPERTS.read_bytes()).hexdigest()
    result['cold'] = 'D9 SYNOP labels, 4.84 C, latest stored version of each station-time (D18)'
    # Files first, so a later failure of the tracking server cannot lose the numbers.
    parts['scored'].to_csv(OUTPUT / 'intervals.csv', index_label='value_time_utc')
    pd.DataFrame({**parts['candidates'], 'actual': parts['frame'].actual,
                  'rte_d1': parts['frame'].rte_d1, RTE_STAR: parts['frame'][RTE_STAR]}).to_csv(
        OUTPUT / 'predictions.csv', index_label='value_time_utc')
    parts['weights'].to_csv(OUTPUT / 'weights.csv')
    (OUTPUT / 'results.json').write_text(json.dumps(result, indent=2, default=str), encoding='utf-8')
    print_summary(result)
    log_results(result, parts, cold, sources)


def log_results(result: dict, parts: dict, cold: dict, sources: list) -> None:
    """One MLflow run: candidate 1 as the model, candidate 2 and the interval as extra metrics."""
    frame = parts['frame']
    common = parts['common']
    one = result['candidate1']
    run_id = log_backtest(
        one | {'n_excluded': result['n_excluded']}, TEST_FIRST, TEST_LAST, exp='TEST-2025', frame=frame.loc[common].assign(
            **{ev.MODEL: parts['candidates']['candidate1'].loc[common]}),
        cold=cold, reference=parts['candidates']['candidate2'].loc[common],
        reference_name='candidate 2 (EXP-007)', reference_column=ev.RTE, params={
            'model': 'candidate 1: EXP-009 ML-Poly aggregate + EXP-012 seasonal conformal 80 percent; '
                     'candidate 2: EXP-007 GAM + Kalman; the plots show candidate 1',
            'protocol_commit': result['protocol_commit'], 'decision': 'D18',
            'training_start': '2021-03-23', 'refit': 'weekly Monday cutoff, expanding window',
            'conformal_window_days': WINDOW_DAYS, 'residuals_from': VALIDATION_FIRST,
            'rte_star': 'a2 with the 2024 mean error by local hour',
            'cold_labels': result['cold'], 'bootstrap_seed': ev.BOOTSTRAP_SEED,
            'source_dataset': ','.join(f'{n}:{c}' for n, c in sources) + ';gfs:raw TMP',
            'single_run': 'no rerun, no change after seeing 2025'})
    metrics = {'n_slots_common': result['n_slots_common'], 'n_cold_days': result['n_cold_days']}
    two = result['candidate2']
    for key in ('mae', 'rmse', 'mape'):
        metrics[f'candidate2_{key}'] = two['model'][key]
        metrics[f'candidate1_{key}'] = one['model'][key]
    for tag, block in (('c1', one), ('c2', two)):
        for ref, name in (('', 'rte'), ('rte_star_', 'rte_star')):
            for width in (7, 14):
                for stat in ('mean', 'low', 'high'):
                    metrics[f'{tag}_vs_{name}_bootstrap_{width}_{stat}'] = block[
                        f'{ref}bootstrap_{width}'][stat]
    for tag, block in (('c1', one), ('c2', two)):
        for ref, table in (('rte', block['day_types_vs_rte']), ('rte_star', block['day_types_vs_rte_star'])):
            for kind, row in table.items():
                metrics[f'{tag}_lab_{kind}_model_vs_{ref}'] = row[ev.MODEL]
                metrics[f'{tag}_lab_{kind}_{ref}'] = row[ev.RTE]
                metrics[f'{tag}_lab_{kind}_n_days'] = row['n_days']
    for width in (7, 14):
        for stat in ('mean', 'low', 'high'):
            metrics[f'c1_vs_c2_bootstrap_{width}_{stat}'] = result['candidate1_vs_candidate2'][
                f'bootstrap_{width}'][stat]
    report = result['intervals']
    metrics.update({'interval_coverage': report['coverage'], 'interval_coverage_low': report['low'],
                    'interval_coverage_high': report['high'],
                    'interval_mean_width_mw': report['mean_width_mw'],
                    'seasonal_distance_points': report['seasonal_distance_points']})
    for season, row in report['by_season'].items():
        metrics[f'coverage_{season}'] = row['coverage']
    for kind, row in report['by_day_type'].items():
        metrics[f'coverage_{kind}'] = row['coverage']
    stamp = int(time.time() * 1000)
    mlflow = f'{mlflow_tracking_uri()}/api/2.0/mlflow'
    requests.post(f'{mlflow}/runs/log-batch', json={
        'run_id': run_id, 'metrics': [{'key': k, 'value': float(v), 'timestamp': stamp, 'step': 1}
                                      for k, v in metrics.items()]}, timeout=30).raise_for_status()
    got = requests.get(f'{mlflow}/runs/get', params={'run_id': run_id}, timeout=30)
    got.raise_for_status()
    result['mlflow_run_id'] = run_id
    result['mlflow_status'] = got.json()['run']['info']['status']
    (OUTPUT / 'results.json').write_text(json.dumps(result, indent=2, default=str), encoding='utf-8')
    if result['mlflow_status'] != 'FINISHED':
        raise RuntimeError('MLflow did not confirm the completed run')
    print(json.dumps({'mlflow_run_id': run_id, 'mlflow_status': result['mlflow_status']}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--rehearsal', action='store_true')
    if parser.parse_args().rehearsal:
        rehearsal()
    else:
        final_run()


if __name__ == '__main__':
    main()
