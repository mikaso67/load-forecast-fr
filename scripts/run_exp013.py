"""EXP-013: school holiday share (part b), forecast solar radiation (part a) and both as inputs.

Modes:
  select        2023 inner walk-forward of the EXP-005 GAM, without the share and with each encoding (D13)
  select-solar  the same 2023 run for the solar radiation encodings
  gam, lgbm     2024 backtest of the EXP-005 GAM or EXP-008 LightGBM plus the school share
  gam-solar, lgbm-solar  the same plus solar radiation instead of the share
  gam-both, lgbm-both    solar radiation and the share, against the solar run (single change: the share)
GAM runs recompute EXP-005 in the same run; LightGBM runs read the saved EXP-008 forecast.
Add --dry to print the 2024 results without logging to MLflow or writing files.
"""

import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from run_exp002 import TRAINING_START, load, paired_against_reference
from run_exp003 import SOURCE_QUERY, WEATHER_FIRST, day_type_table, monthly_diagnostic
from run_exp005 import BACKUP_FLAG_005, EXP003_MAE, EXP004_MAE, SMOOTHING_WEIGHT, make_gam
from run_exp007 import EXP005_MAE
from run_exp008 import CONFIG

from load_forecast import evaluation as ev
from load_forecast.backtest import log_backtest, score_backtest, walk_forward
from load_forecast.baseline import FIRST, LAST
from load_forecast.database import clean_query
from load_forecast.eco2mix import connect_database
from load_forecast.gam_model import lagged_gam_features
from load_forecast.gbm_model import booster_params, walk_forward_global
from load_forecast.labels import read_labels
from load_forecast.lag_model import BACKUP_FLAG, lagged_ols_features
from load_forecast.linear_model import SlotOLS, calendar_ols_features
from load_forecast.rte_reference import RTE_STAR, prior_year_hourly_reference
from load_forecast.school_holidays import (
    ENCODINGS,
    SHARE_COLUMN,
    holiday_share,
    read_department_population,
    read_holiday_periods,
    with_school_holidays,
    zone_weights,
)
from load_forecast.solar_features import ENCODINGS as SOLAR_ENCODINGS
from load_forecast.solar_features import SOLAR_COLUMN, read_solar, with_solar
from load_forecast.temperature_model import temperature_ols_features, with_calendar_backup
from load_forecast.weather_features import read_temperature

OUTPUT = Path('data/experiments/EXP-013')
CALENDAR_FILE = Path('data/external/school_calendar/fr-en-calendrier-scolaire.csv')
POPULATION_FILE = Path('data/external/insee_population/donnees_departements.csv')
SCHOOL_YEARS = (2020, 2024)  # school years 2020-2021 to 2024-2025
INNER_FIRST, INNER_LAST = date(2023, 1, 1), date(2023, 12, 31)
EXP005_2023_MAE = 1057.0
EXP008_MAE = 1113.94
EXP008_FILE = Path('data/experiments/EXP-008/backtest_predictions.csv')
# Chosen on 2023 only by `run_exp013.py select` and recorded in EXP-013.md before any 2024 run.
ENCODING: str | None = 'working_days'  # 2023 MAE 1064.3 MW, all_days 1068.0, no input 1057.0
# Chosen on 2023 only by `run_exp013.py select-solar` and recorded in EXP-013.md before any 2024 run.
SOLAR_ENCODING: str | None = 'daily'  # 2023 MAE 1056.09 MW, slot 1056.15, no input 1057.00
BACKUP_FLAG_013 = 'backup_reference'
INPUTS = ('school', 'solar', 'both')
LOCK_END = pd.Timestamp('2025-01-01', tz='Europe/Paris').tz_convert('UTC')


def assert_no_2025(*indexes: pd.Index) -> None:
    for index in indexes:
        if len(index) and index.max() >= LOCK_END:
            raise RuntimeError('A 2025 timestamp reached EXP-013')


def school_share() -> pd.Series:
    periods = read_holiday_periods(CALENDAR_FILE, *SCHOOL_YEARS)
    weights = zone_weights(periods, read_department_population(POPULATION_FILE))
    print('zone weights', {k: round(v, 5) for k, v in weights.items()}, flush=True)
    return holiday_share(pd.date_range(TRAINING_START, LAST).date, periods, weights)


def select() -> None:
    data = load()
    end = pd.Timestamp(INNER_LAST + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
    history = data.consommation.loc[data.index < end]
    with connect_database() as connection:
        temperature = read_temperature(connection, WEATHER_FIRST, INNER_LAST)
    assert_no_2025(history.index, temperature.index)
    share = school_share()
    base = lagged_gam_features(temperature)
    candidates = {'none': base} | {e: with_school_holidays(base, share, e) for e in ENCODINGS}
    rows = {}
    for name, builder in candidates.items():
        began = time.perf_counter()
        predictions = walk_forward(history, INNER_FIRST, INNER_LAST, make_gam, builder=builder,
                                   training_start=TRAINING_START)
        kept = predictions[[ev.MODEL]].assign(**{ev.ACTUAL: history}).dropna()
        rows[name] = {'mae_2023': float((kept[ev.MODEL] - kept[ev.ACTUAL]).abs().mean()),
                      'n_slots': int(len(kept)), 'seconds': round(time.perf_counter() - began, 1)}
        print(name, rows[name], flush=True)
    if abs(rows['none']['mae_2023'] - EXP005_2023_MAE) > 0.05:
        raise RuntimeError('The 2023 EXP-005 GAM did not reproduce')
    print(json.dumps({'inner_period': '2023', 'rows': rows}))


def read_checked_solar(connection, last: date) -> pd.DataFrame:
    """Solar radiation from WEATHER_FIRST; only the days recorded as weather missing may be NaN."""
    solar = read_solar(connection, WEATHER_FIRST, last)
    with connection.cursor() as cursor:
        cursor.execute("SELECT delivery_day FROM raw.gfs_days WHERE status = 'weather missing' "
                       'AND delivery_day BETWEEN %s AND %s', (WEATHER_FIRST, last))
        missing = {row[0] for row in cursor.fetchall()}
    empty = set(ev.local_dates(solar.index[solar[SOLAR_COLUMN].isna()]))
    if empty - missing:
        raise RuntimeError(f'Days with a selected run but no radiation: {sorted(empty - missing)[:10]}')
    print('solar days without weather (rule: NaN, backup model):', sorted(str(d) for d in empty),
          flush=True)
    return solar


def select_solar() -> None:
    data = load()
    end = pd.Timestamp(INNER_LAST + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
    history = data.consommation.loc[data.index < end]
    with connect_database() as connection:
        temperature = read_temperature(connection, WEATHER_FIRST, INNER_LAST)
        solar = read_checked_solar(connection, INNER_LAST)
    assert_no_2025(history.index, temperature.index, solar.index)
    base = lagged_gam_features(temperature)
    candidates = {'none': base} | {e: with_solar(base, solar, e) for e in SOLAR_ENCODINGS}
    rows = {}
    for name, builder in candidates.items():
        began = time.perf_counter()
        predictions = walk_forward(history, INNER_FIRST, INNER_LAST, make_gam, builder=builder,
                                   training_start=TRAINING_START)
        kept = predictions[[ev.MODEL]].assign(**{ev.ACTUAL: history}).dropna()
        rows[name] = {'mae_2023': float((kept[ev.MODEL] - kept[ev.ACTUAL]).abs().mean()),
                      'n_slots': int(len(kept)), 'seconds': round(time.perf_counter() - began, 1)}
        print(name, rows[name], flush=True)
    if abs(rows['none']['mae_2023'] - EXP005_2023_MAE) > 0.05:
        raise RuntimeError('The 2023 EXP-005 GAM did not reproduce')
    print(json.dumps({'inner_period': '2023', 'input': 'solar', 'rows': rows}))


def reference_chain(data: pd.DataFrame, temperature: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """EXP-004 and EXP-005 exactly as in run_exp005.py, checked against their published MAE."""
    calendar = walk_forward(data.consommation, FIRST, LAST, SlotOLS,
                            builder=calendar_ols_features, training_start=TRAINING_START)
    weather = walk_forward(data.consommation, FIRST, LAST, SlotOLS,
                           builder=temperature_ols_features(temperature), training_start=TRAINING_START)
    exp003 = with_calendar_backup(weather, calendar)
    lagged = walk_forward(data.consommation, FIRST, LAST, SlotOLS,
                          builder=lagged_ols_features(temperature), training_start=TRAINING_START)
    exp004 = with_calendar_backup(lagged, exp003, flag=BACKUP_FLAG)
    gam = walk_forward(data.consommation, FIRST, LAST, make_gam,
                       builder=lagged_gam_features(temperature), training_start=TRAINING_START)
    exp005 = with_calendar_backup(gam, exp004, flag=BACKUP_FLAG_005)
    for name, forecast, expected in (('EXP-003', exp003, EXP003_MAE), ('EXP-004', exp004, EXP004_MAE),
                                     ('EXP-005', exp005, EXP005_MAE)):
        check_mae(name, forecast[ev.MODEL], data, expected)
    return exp004, exp005


def check_mae(name: str, model: pd.Series, data: pd.DataFrame, expected: float) -> None:
    frame = pd.DataFrame({ev.MODEL: model, ev.ACTUAL: data.consommation, ev.RTE: data.prevision_j1})
    mae = ev.score(frame).model.mae
    if abs(mae - expected) > 0.01:
        raise RuntimeError(f'{name} did not reproduce: {mae:.2f} instead of {expected}')
    print(name, 'reproduced, MAE', round(mae, 2), flush=True)


def mixed_days_difference(model: pd.Series, reference: pd.Series, data: pd.DataFrame,
                          share: pd.Series) -> dict:
    """Descriptive: mean daily MAE difference on the days where only some zones are on holiday."""
    frame = pd.DataFrame({'model': model, 'reference': reference, ev.ACTUAL: data.consommation,
                          ev.RTE: data.prevision_j1}).dropna()
    frame = frame.loc[(frame.index >= pd.Timestamp(FIRST, tz='Europe/Paris'))]
    daily = frame[['model', 'reference']].sub(frame[ev.ACTUAL], axis=0).abs().groupby(
        ev.local_dates(frame.index)).mean()
    mixed = [d for d in daily.index if 0 < share[d] < 1]
    difference = daily.loc[mixed, 'model'] - daily.loc[mixed, 'reference']
    return {'n_days': len(mixed), 'mean_daily_difference_mw': float(difference.mean()),
            'n_days_model_better': int((difference < 0).sum())}


def read_predictions(path: Path) -> pd.Series:
    saved = pd.read_csv(path, index_col='value_time_utc')
    saved.index = pd.DatetimeIndex(pd.to_datetime(saved.index, utc=True))
    return saved[ev.MODEL]


def output_name(expert: str, inputs: str) -> str:
    return expert if inputs == 'school' else f'{expert}_{inputs}'


def backtest(expert: str, inputs: str = 'school') -> None:
    if inputs != 'solar' and ENCODING not in ENCODINGS:
        raise RuntimeError('Encoding not chosen on 2023 yet: run `run_exp013.py select` first')
    if inputs != 'school' and SOLAR_ENCODING not in SOLAR_ENCODINGS:
        raise RuntimeError('Solar encoding not chosen on 2023 yet: run `run_exp013.py select-solar`')
    solar_file = OUTPUT / f'{output_name(expert, "solar")}_predictions.csv'
    if inputs == 'both' and not solar_file.exists():
        raise RuntimeError(f'Run the solar backtest first: {solar_file} is missing')
    data = load()
    with connect_database() as connection:
        temperature = read_temperature(connection, WEATHER_FIRST, LAST)
        labels = read_labels(connection, FIRST, LAST + timedelta(days=1))
        start = pd.Timestamp(FIRST, tz='Europe/Paris').tz_convert('UTC')
        sources = connection.execute(clean_query(SOURCE_QUERY), {'start': start, 'end': LOCK_END}).fetchall()
        solar = read_checked_solar(connection, LAST) if inputs != 'school' else None
    assert_no_2025(data.index, temperature.index, *([] if solar is None else [solar.index]))
    days = pd.Series(ev.local_dates(temperature.index), index=temperature.index)
    status = temperature['weather_status'].groupby(days).first().value_counts().to_dict()
    if status.get('not_collected'):
        raise RuntimeError(f'Days without collected TMP rows: {status}')
    share = school_share()
    builder = lagged_gam_features(temperature)
    if inputs != 'school':
        builder = with_solar(builder, solar, SOLAR_ENCODING)
    if inputs != 'solar':
        builder = with_school_holidays(builder, share, ENCODING)
    exp004, exp005 = reference_chain(data, temperature)

    began = time.perf_counter()
    if expert == 'gam':
        raw = walk_forward(data.consommation, FIRST, LAST, make_gam, builder=builder,
                           training_start=TRAINING_START)
        predictions = with_calendar_backup(raw, exp004, flag=BACKUP_FLAG_013)
        reference, reference_name = exp005[ev.MODEL], 'EXP-005'
    else:
        def progress(refit, model) -> None:
            print('refit', refit, 'trees', model.booster_.num_trees(),
                  'elapsed s', round(time.perf_counter() - began), flush=True)

        raw = walk_forward_global(data.consommation, FIRST, LAST, CONFIG, builder=builder,
                                  training_start=TRAINING_START, on_refit=progress)[CONFIG.rounds]
        predictions = with_calendar_backup(raw, exp005, flag=BACKUP_FLAG_013)
        reference, reference_name = read_predictions(EXP008_FILE), 'EXP-008'
        check_mae('EXP-008 (saved)', reference, data, EXP008_MAE)
    seconds = round(time.perf_counter() - began, 1)
    assert_no_2025(predictions.index)
    flagged = predictions[BACKUP_FLAG_013]
    flagged_days = sorted(set(ev.local_dates(predictions.index[flagged])))
    print(expert, 'walk-forward seconds', seconds, '| backup slots', int(flagged.sum()),
          '| days', [str(d) for d in flagged_days], flush=True)
    if predictions[ev.MODEL].isna().any():
        raise RuntimeError('Forecast slots without any model')

    frame = predictions.assign(actual=data.consommation, rte_d1=data.prevision_j1)
    frame[RTE_STAR] = prior_year_hourly_reference(data.consommation, data.prevision_j1, frame[ev.RTE])
    result = score_backtest(predictions, data.consommation, data.prevision_j1, frame[RTE_STAR])
    if inputs == 'both':
        # Single change against the solar run: the school share. The expert reference is kept too.
        result['vs_expert_reference'] = paired_against_reference(predictions[ev.MODEL], reference, data)
        result['expert_reference'] = reference_name
        reference, reference_name = read_predictions(solar_file), f'EXP-013 {expert} solar'
    result['vs_reference'] = paired_against_reference(predictions[ev.MODEL], reference, data)
    result['reference'] = reference_name
    result['mixed_days'] = mixed_days_difference(predictions[ev.MODEL], reference, data, share)
    result['walk_forward_seconds'] = seconds
    result['inputs'] = inputs
    result['encoding'] = ENCODING if inputs != 'solar' else None
    result['solar_encoding'] = SOLAR_ENCODING if inputs != 'school' else None

    cold = {d: bool(flag) for d, flag in labels.cold.items() if not pd.isna(flag)}
    tables = {'model_vs_rte': day_type_table(predictions[ev.MODEL], data.consommation,
                                             data.prevision_j1, cold),
              'reference_vs_rte': day_type_table(reference, data.consommation, data.prevision_j1, cold),
              'model_vs_rte_star': day_type_table(predictions[ev.MODEL], data.consommation,
                                                  frame[RTE_STAR], cold)}
    for name, table in tables.items():
        print(name)
        print(table.round(2).to_string())
    result['day_type_tables'] = {k: v.to_dict(orient='index') for k, v in tables.items()}
    result['n_cold_days'] = sum(cold.values())
    result['label_missing_days'] = [str(d) for d, flag in labels.cold.items() if pd.isna(flag)]
    result['n_backup_slots'] = int(flagged.sum())
    result['backup_days'] = [str(d) for d in flagged_days]
    monthly = pd.concat({name: monthly_diagnostic(pd.DataFrame({
        ev.MODEL: f, ev.ACTUAL: data.consommation, ev.RTE: data.prevision_j1}))
        for name, f in (('exp013', predictions[ev.MODEL]), ('reference', reference))}, axis=1)
    print(monthly.to_string())
    result['monthly'] = json.loads(monthly.to_json())

    keys = ('model', 'rte', RTE_STAR, 'bootstrap_7', 'rte_star_bootstrap_7', 'vs_reference',
            'mixed_days') + (('vs_expert_reference',) if inputs == 'both' else ())
    print(json.dumps({k: result[k] for k in keys}, indent=2, default=str))
    if '--dry' in sys.argv:
        return
    added = {'school': f'{SHARE_COLUMN} ({ENCODING})', 'solar': f'{SOLAR_COLUMN} ({SOLAR_ENCODING})',
             'both': f'{SOLAR_COLUMN} ({SOLAR_ENCODING}) and {SHARE_COLUMN} ({ENCODING})'}[inputs]
    model_text = (f'EXP-005 GAM per local slot plus linear terms: {added}' if expert == 'gam'
                  else f'EXP-008 global LightGBM plus columns: {added}')
    change = f'add {SHARE_COLUMN} to the solar run' if inputs == 'both' else f'add {added}'
    params = {
        'model': model_text, 'single_change': change, 'inputs': inputs,
        'calendar_source': 'data.education.gouv.fr fr-en-calendrier-scolaire, downloaded 2026-10-16, '
                           'sha256 f08a562be938a1aa1dec2f32d8d3ea274b7d8050c1f0d17eb9a859108ce37baa',
        'population_source': 'INSEE populations legales 2021 donnees_departements.csv PMUN, '
                             'sha256 0515ca6b83370eb50cef2067b69ab6eb9b75b1af4f52506ef53123d98bb68d73',
        'encoding_rule': 'chosen on the 2023 inner walk-forward of the GAM, frozen before 2024',
        'gam_smoothing_weight': SMOOTHING_WEIGHT, 'training_start': TRAINING_START,
        'refit': 'weekly, Monday cutoff, expanding window', 'bootstrap_seed': ev.BOOTSTRAP_SEED,
        'reference': reference_name,
        'source_dataset': ','.join(f'{n}:{c}' for n, c in sources)
                          + (';gfs:raw.gfs_points TMP' if inputs == 'school' else ';gfs:raw.gfs_points TMP,DSWRF')}
    if inputs != 'school':
        params['solar_rule'] = ('DSWRF de-accumulated to hourly means, stamped at mid-hour, interpolated '
                                'to 30 min, equal-weight mean of the 12 points; weather missing days '
                                'use the backup')
    if expert == 'lgbm':
        params |= {k: v for k, v in booster_params(CONFIG).items() if k != 'metric'}
        params['rounds'] = CONFIG.rounds
    result['mlflow_run_id'] = log_backtest(
        result, FIRST, LAST, exp='EXP-013', ticket=os.environ.get('LFF_TICKET'), frame=frame,
        cold=cold, reference=reference, reference_name=reference_name, params=params)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    name = output_name(expert, inputs)
    predictions.to_csv(OUTPUT / f'{name}_predictions.csv', index_label='value_time_utc')
    (OUTPUT / f'{name}_results.json').write_text(json.dumps(result, indent=2, default=str),
                                                   encoding='utf-8')
    print('mlflow run', result['mlflow_run_id'])


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    expert_name, _, input_name = mode.partition('-')
    if mode == 'select':
        select()
    elif mode == 'select-solar':
        select_solar()
    elif expert_name in ('gam', 'lgbm') and (input_name or 'school') in INPUTS:
        backtest(expert_name, input_name or 'school')
    else:
        raise SystemExit(__doc__)
