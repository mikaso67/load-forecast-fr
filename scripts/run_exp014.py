"""EXP-014: does the part of D-1 known at the cutoff (D2, up to 09:30 Paris) speed up the online layer?

Three variants, each compared with EXP-009 on the same slots (pre-registered in EXP-014.md):
- A1: the ML-Poly aggregation also absorbs the losses of the known D-1 morning slots;
- A2: the per-slot Kalman of EXP-007 absorbs the known D-1 morning slots one day earlier;
- A3: the EXP-009 aggregate is shifted by a shrunk mean error of the known D-1 morning slots.

`select` works on 2023 only (consumption cut after 2023-12-31, weather read to 2023-12-31) and fixes
the A3 shrinkage. `run` evaluates on 2024 and refuses to start unless that choice is committed.
Nothing here reads 2025.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from expert_forecasts import EXPERTS
from run_exp002 import TRAINING_START, load, paired_against_reference
from run_exp003 import SOURCE_QUERY, WEATHER_FIRST, day_type_table
from run_exp006 import make_gam1
from run_exp007 import FILTER_MODE
from run_exp009 import EXPECTED
from select_aggregation import CACHE as CACHE_2023

from load_forecast import evaluation as ev
from load_forecast.aggregation import aggregate, morning_level_shift
from load_forecast.backtest import log_backtest, score_backtest
from load_forecast.baseline import FIRST, LAST
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.experiment_reports import committed_report
from load_forecast.gam_model import lagged_gam_features
from load_forecast.kalman import walk_forward_kalman
from load_forecast.labels import read_labels
from load_forecast.rte_reference import RTE_STAR, prior_year_hourly_reference
from load_forecast.weather_features import read_temperature

OUTPUT = Path('data/experiments/EXP-014')
CACHE_2024 = Path('data/experiments/EXP-009/experts_2024.csv')
MORNING_2023 = OUTPUT / 'exp007_morning_2023.csv'
FIRST_2023, LAST_2023 = date(2023, 1, 1), date(2023, 12, 31)
LOCK_END = pd.Timestamp('2025-01-01', tz='Europe/Paris').tz_convert('UTC')
EXP009_MAE_2024 = 828.52
EXP009_MAE_2023 = 844.65
SHRINK_GRID = tuple(round(0.1 * k, 1) for k in range(11))
TIE_MW = 1.0
TRANSITION_C = 3.0
WINDOW = (date(2024, 10, 1), date(2024, 11, 30))
# Chosen on 2023 only by `run_exp014.py select` and committed in EXP-014.md before any 2024 run.
SHRINK: float | None = 0.3  # 2023 MAE 814.16 MW, EXP-009 844.65
CHOICE_COMMIT: str | None = '0ad5898951bcf7923507a93b344deca04f3f0727'


def assert_no_2025(*indexes: pd.Index) -> None:
    for index in indexes:
        if len(index) and index.max() >= LOCK_END:
            raise RuntimeError('A 2025 timestamp reached EXP-014')


def mae(forecast: pd.Series, actual: pd.Series) -> float:
    kept = pd.DataFrame({'f': forecast, 'y': actual}).dropna()
    return float((kept.f - kept.y).abs().mean())


def read_experts(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, index_col=0, parse_dates=True)[list(EXPERTS)]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def morning_kalman(history: pd.Series, first: date, last: date, experts: pd.DataFrame) -> tuple:
    """EXP-007 with the morning update, its EXP-005 backup, and checks against the frozen experts.

    The GAM must equal the frozen EXP-005, and every slot the morning cannot reach (local 10:00 or
    later) must equal the frozen EXP-007: the variant may only move the known morning slots.
    """
    with connect_database() as connection:
        temperature = read_temperature(connection, WEATHER_FIRST, last)
    assert_no_2025(temperature.index, history.index)
    result = walk_forward_kalman(history, first, last, make_gam1,
                                 builder=lagged_gam_features(temperature),
                                 training_start=TRAINING_START, modes=(FILTER_MODE,), morning=True)
    gam = result.gam[ev.MODEL].reindex(experts.index)
    fast = result.corrected[FILTER_MODE][ev.MODEL].reindex(experts.index)
    backup = fast.isna()
    fast = fast.where(~backup, experts['EXP-005'])
    gam_gap = float((gam - experts['EXP-005']).abs().max())
    local = experts.index.tz_convert('Europe/Paris')
    late = np.asarray(local.hour >= 10)
    late_gap = float((fast[late] - experts['EXP-007'][late]).abs().max())
    moved = (fast - experts['EXP-007']).abs() > 1e-6
    checks = {'gam_max_abs_gap_mw': gam_gap, 'late_slots_max_abs_gap_mw': late_gap,
              'n_backup_slots': int(backup.sum()), 'n_moved_slots': int(moved.sum()),
              'latest_moved_local_time': str(local[moved.to_numpy()].time.max()) if moved.any() else None}
    if gam_gap > 0.01 or late_gap > 0.01:
        raise RuntimeError(f'The morning Kalman does not reproduce the frozen experts: {checks}')
    print('A2 checks', checks, flush=True)
    return fast.rename('EXP-007m'), checks


def select() -> None:
    """2023 only: reproduce EXP-009, score A1 and A2, choose the A3 shrinkage."""
    data = load()
    end = pd.Timestamp(LAST_2023 + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
    history = data.consommation.loc[data.index < end]
    experts = read_experts(CACHE_2023)
    assert_no_2025(history.index, experts.index)
    reference = aggregate(experts, history, per_slot=False).forecast
    rows = {'EXP-009': mae(reference, history)}
    if abs(rows['EXP-009'] - EXP009_MAE_2023) > 0.01:
        raise RuntimeError(f'EXP-009 2023 did not reproduce: {rows["EXP-009"]:.3f}')
    rows['A1'] = mae(aggregate(experts, history, per_slot=False, morning=True).forecast, history)
    shrink = {s: mae(morning_level_shift(reference, history, s), history) for s in SHRINK_GRID}
    best = min(shrink.values())
    choice = min(s for s, value in shrink.items() if value <= best + TIE_MW)
    began = time.perf_counter()
    fast, checks = morning_kalman(history, FIRST_2023, LAST_2023, experts)
    seconds = round(time.perf_counter() - began, 1)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    fast.to_frame().to_csv(MORNING_2023, index_label='value_time_utc')
    rows['A2'] = mae(aggregate(experts.assign(**{'EXP-007': fast}), history,
                               per_slot=False).forecast, history)
    report = {'inner_period': '2023', 'mae_2023': rows, 'a3_shrink_mae_2023': shrink,
              'a3_choice': choice, 'exp007_2023': mae(experts['EXP-007'], history),
              'exp007_morning_2023': mae(fast, history), 'a2_checks': checks,
              'kalman_seconds': seconds, 'expert_cache_sha256': sha256(CACHE_2023),
              'morning_2023_sha256': sha256(MORNING_2023)}
    (OUTPUT / 'select_2023.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


def check_choice() -> None:
    if SHRINK is None or CHOICE_COMMIT is None:
        raise RuntimeError('The 2023 choice is not recorded: run select and commit it first')
    ancestor = subprocess.run(['git', 'merge-base', '--is-ancestor', CHOICE_COMMIT, 'HEAD'],
                              check=False)
    if ancestor.returncode != 0:
        raise RuntimeError('The 2023 choice commit is not in the history of HEAD')
    if f'the shrinkage is **{SHRINK}**' not in committed_report(CHOICE_COMMIT, 'EXP-014.md'):
        raise RuntimeError('The committed EXP-014.md does not record this shrinkage')


def subset_difference(model: pd.Series, reference: pd.Series, data: pd.DataFrame,
                      days: set[date], first: date, last: date) -> dict:
    """Paired daily MAE difference on a subset of days, other days NaN in the calendar (descriptive)."""
    frame = pd.DataFrame({ev.MODEL: model, ev.ACTUAL: data.consommation, ev.RTE: data.prevision_j1})
    ref = frame.assign(**{ev.MODEL: reference.reindex(frame.index)})
    daily = ev.daily_mae(frame)[ev.MODEL] - ev.daily_mae(ref)[ev.MODEL]
    daily.index = pd.DatetimeIndex(daily.index)
    calendar = pd.date_range(first, last, freq='D')
    kept = daily.reindex(calendar).where(calendar.isin(pd.DatetimeIndex(sorted(days))))
    boot = ev.block_bootstrap(kept)
    signed = (frame[ev.MODEL] - frame[ev.ACTUAL]).groupby(ev.local_dates(frame.index)).mean()
    ref_signed = (ref[ev.MODEL] - ref[ev.ACTUAL]).groupby(ev.local_dates(ref.index)).mean()
    chosen = [d for d in signed.index if d in days]
    return {'n_days': boot.n_days, 'mean_daily_mae_difference': boot.mean,
            'low_7': boot.low, 'high_7': boot.high,
            'mean_signed_error_model': float(signed.loc[chosen].mean()),
            'mean_signed_error_reference': float(ref_signed.loc[chosen].mean())}


def transition_days(labels: pd.DataFrame) -> tuple[set[date], int]:
    """2024 days whose observed national mean differs by more than 3 C from the mean of the 7 days
    before (SYNOP labels, D9 source); at least 5 of those 7 days must be labelled."""
    temperature = labels['national_temperature_c'].astype(float)
    temperature.index = pd.DatetimeIndex(temperature.index)
    before = temperature.shift(1).rolling(7, min_periods=5).mean()
    change = (temperature - before).abs()
    kept = change.loc[(change.index >= pd.Timestamp(FIRST)) & (change.index <= pd.Timestamp(LAST))]
    days = {d.date() for d, v in kept.items() if v > TRANSITION_C}
    return days, int(kept.isna().sum())


def run() -> None:
    check_choice()
    data = load()
    assert_no_2025(data.index)
    with connect_database() as connection:
        labels = read_labels(connection, FIRST - timedelta(days=7), LAST + timedelta(days=1))
        start = pd.Timestamp(FIRST, tz='Europe/Paris').tz_convert('UTC')
        end = pd.Timestamp(LAST + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
        sources = connection.execute(clean_query(SOURCE_QUERY), {'start': start, 'end': end}).fetchall()
    experts_2023, experts_2024 = read_experts(CACHE_2023), read_experts(CACHE_2024)
    assert_no_2025(experts_2024.index)
    for name in EXPERTS:
        frame = pd.DataFrame({ev.MODEL: experts_2024[name], ev.ACTUAL: data.consommation,
                              ev.RTE: data.prevision_j1})
        if abs(ev.score(frame).model.mae - EXPECTED[name]) > 0.01:
            raise RuntimeError(f'{name} 2024 cache does not reproduce')
    experts = pd.concat([experts_2023, experts_2024])
    in_2024 = experts.index >= start
    actual = data.consommation
    reference_chain = aggregate(experts, actual, per_slot=False).forecast
    reference = reference_chain.loc[in_2024]
    frame = pd.DataFrame({ev.MODEL: reference, ev.ACTUAL: actual, ev.RTE: data.prevision_j1})
    if abs(ev.score(frame).model.mae - EXP009_MAE_2024) > 0.01:
        raise RuntimeError('EXP-009 2024 did not reproduce')

    began = time.perf_counter()
    fast_2024, checks = morning_kalman(actual, FIRST, LAST, experts_2024)
    seconds = round(time.perf_counter() - began, 1)
    fast_2023 = pd.read_csv(MORNING_2023, index_col=0, parse_dates=True)['EXP-007m']
    fast = pd.concat([fast_2023, fast_2024])
    variants = {
        'A1': aggregate(experts, actual, per_slot=False, morning=True).forecast.loc[in_2024],
        'A2': aggregate(experts.assign(**{'EXP-007': fast}), actual,
                        per_slot=False).forecast.loc[in_2024]}
    if SHRINK > 0:
        variants['A3'] = morning_level_shift(reference_chain, actual, SHRINK).loc[in_2024]

    cold = {d: bool(flag) for d, flag in labels.cold.items() if not pd.isna(flag) and d >= FIRST}
    transitions, unlabelled = transition_days(labels)
    window_days = {d.date() for d in pd.date_range(*WINDOW)}
    rte_star = prior_year_hourly_reference(actual, data.prevision_j1,
                                          data.prevision_j1.reindex(reference.index))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    summary = {'n_transition_days': len(transitions), 'transition_days': sorted(map(str, transitions)),
               'transition_unlabelled_days': unlabelled, 'a2_checks_2024': checks,
               'kalman_seconds': seconds, 'shrink': SHRINK, 'choice_commit': CHOICE_COMMIT,
               'expert_cache_sha256': {'2023': sha256(CACHE_2023), '2024': sha256(CACHE_2024)},
               'morning_2023_sha256': sha256(MORNING_2023),
               'exp007_morning_vs_exp007': paired_against_reference(
                   fast_2024, experts_2024['EXP-007'], data)}
    summary['reference_descriptive'] = {
        'window': subset_difference(reference, reference, data, window_days, FIRST, LAST),
        'transition': subset_difference(reference, reference, data, transitions, FIRST, LAST)}
    for name, forecast in variants.items():
        predictions = forecast.to_frame(ev.MODEL)
        result = score_backtest(predictions, actual, data.prevision_j1, rte_star)
        result['vs_EXP-009'] = paired_against_reference(forecast, reference, data)
        result['window_oct_nov'] = subset_difference(forecast, reference, data, window_days,
                                                     FIRST, LAST)
        result['transition_days'] = subset_difference(forecast, reference, data, transitions,
                                                      FIRST, LAST)
        tables = {'vs_rte': day_type_table(forecast, actual, data.prevision_j1, cold),
                  'vs_rte_star': day_type_table(forecast, actual, rte_star, cold),
                  'exp009_vs_rte': day_type_table(reference, actual, data.prevision_j1, cold)}
        result['day_type_tables'] = {k: v.to_dict(orient='index') for k, v in tables.items()}
        for label, table in tables.items():
            print(name, label)
            print(table.round(2).to_string())
        printed = {k: result[k] for k in ('model', 'rte', 'vs_EXP-009', 'window_oct_nov',
                                          'transition_days')}
        print(name, json.dumps(printed, indent=2, default=str), flush=True)
        if '--dry' not in sys.argv:
            scored = predictions.assign(actual=actual, rte_d1=data.prevision_j1)
            scored[RTE_STAR] = rte_star
            result['mlflow_run_id'] = log_backtest(
                result, FIRST, LAST, exp='EXP-014', ticket=os.environ.get('LFF_TICKET'),
                frame=scored, cold=cold, reference=reference, reference_name='EXP-009', params={
                    'variant': name, 'reference': 'EXP-009', 'shrink': SHRINK,
                    'choice_commit': CHOICE_COMMIT, 'cutoff_rule': 'D2, D-1 slots <= 09:30 Paris',
                    'update_rule': {'A1': 'ML-Poly also absorbs known D-1 morning losses',
                                    'A2': 'EXP-007 slot filters absorb known D-1 morning slots',
                                    'A3': 'aggregate minus shrink x mean D-1 morning error'}[name],
                    'source_dataset': ','.join(f'{n}:{c}' for n, c in sources),
                    'bootstrap_seed': ev.BOOTSTRAP_SEED})
            predictions.to_csv(OUTPUT / f'{name}_predictions.csv', index_label='value_time_utc')
        summary[name] = result
    print(json.dumps({k: summary[k] for k in summary if k not in variants}, indent=2, default=str))
    if '--dry' not in sys.argv:
        (OUTPUT / 'results.json').write_text(json.dumps(summary, indent=2, default=str),
                                             encoding='utf-8')


if __name__ == '__main__':
    {'select': select, 'run': run}[sys.argv[1]]()
