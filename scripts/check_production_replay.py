"""Acceptance test of the production function (docs/production_design.md section 2.5).

Replays 2024 day by day with the production code, after seeding ML-Poly and the conformal pool from
the archived 2023 burn-in, saving each day's state and starting the next day from the reloaded
file. The criterion is the one of D22 (docs/production_design.md section 2.5): the MAE of each
expert and of the aggregate to 0.01 MW, the conformal pool sizes exactly, and each of the four
experts, the aggregate and the intervals compared slot by slot with the saved experiment outputs to
0.1 MW. Read-only on the database; no 2025 day is read or forecast (the experiment lock of
periods.py applies).

Usage: python scripts/check_production_replay.py [state folder]
       python scripts/check_production_replay.py --saved [replay folder]
The second form evaluates the replay_2024.csv already saved in the folder, with no new replay.
"""

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd
from expert_forecasts import EXPERTS as SCRIPT_EXPERTS
from run_exp002 import TRAINING_START, load
from run_exp003 import WEATHER_FIRST
from run_exp005 import DEGREES_OF_FREEDOM, SMOOTHING_WEIGHT
from run_exp007 import FILTER_MODE
from run_exp008 import CONFIG
from run_exp009 import EXPECTED
from run_exp009 import VARIANT as AGGREGATION_VARIANT
from run_exp010 import EXP009_MAE, EXP009_PREDICTIONS
from run_exp012 import SEASON_WINDOW_DAYS
from select_aggregation import CACHE

from load_forecast import evaluation as ev
from load_forecast.baseline import FIRST, LAST
from load_forecast.eco2mix import connect_database
from load_forecast.production import EXPERTS, FROZEN, StateStore, replay, seed_state
from load_forecast.weather_features import read_temperature

EXP009_EXPERTS = Path('data/experiments/EXP-009/experts_2024.csv')
EXP012_INTERVALS = Path('data/experiments/EXP-012/intervals.csv')
DEFAULT_FOLDER = Path('data/production_replay/2024')
MAE_TOLERANCE = 0.01
SLOT_TOLERANCE = 0.1


def check_settings() -> None:
    """The production settings are the ones the experiment scripts ran with."""
    frozen = {'training_start': TRAINING_START, 'gam_degrees_of_freedom': DEGREES_OF_FREEDOM,
              'gam_smoothing_weight': SMOOTHING_WEIGHT, 'gbm': CONFIG,
              'season_window_days': SEASON_WINDOW_DAYS, 'level': 0.8}
    if {k: getattr(FROZEN, k) for k in frozen} != frozen:
        raise RuntimeError('Production settings differ from the experiment scripts')
    if EXPERTS != SCRIPT_EXPERTS or FILTER_MODE != 'restart' or AGGREGATION_VARIANT != 'shared':
        raise RuntimeError('Production experts, Kalman mode or aggregation variant differ')


def mae(forecast: pd.Series, data: pd.DataFrame) -> float:
    frame = pd.DataFrame({ev.MODEL: forecast, ev.ACTUAL: data.consommation,
                          ev.RTE: data.prevision_j1})
    return ev.score(frame).model.mae


def max_abs_difference(saved: pd.DataFrame, replay: pd.DataFrame) -> float:
    """Largest slot difference over the columns of `saved`, on exactly the saved slots."""
    if not replay.index.is_unique:
        raise RuntimeError('The replay holds duplicate slots')
    missing = saved.index.difference(replay.index)
    if len(missing):
        raise RuntimeError(f'The replay misses {len(missing)} saved slots')
    return float((replay.loc[saved.index, saved.columns] - saved).abs().max().max())


def evaluate(out: pd.DataFrame, data: pd.DataFrame) -> dict:
    """The D22 criterion of section 2.5 applied to a replay of 2024."""
    result = {'n_slots': len(out), 'mae': {}, 'mae_ok': {}}
    for name, expected in [*EXPECTED.items(), ('EXP-009', EXP009_MAE)]:
        value = mae(out[ev.MODEL if name == 'EXP-009' else name], data)
        result['mae'][name] = value
        result['mae_ok'][name] = abs(value - expected) <= MAE_TOLERANCE

    experts = pd.read_csv(EXP009_EXPERTS, index_col=0, parse_dates=True)[list(EXPERTS)]
    result['experts_max_abs_difference_mw'] = {
        name: max_abs_difference(experts[[name]], out) for name in EXPERTS}
    saved = pd.read_csv(EXP009_PREDICTIONS, index_col=0, parse_dates=True)[[ev.MODEL]]
    if not saved.index.equals(out.index):
        raise RuntimeError('The replay and EXP-009 do not cover the same slots')
    result['aggregate_max_abs_difference_mw'] = max_abs_difference(saved, out)

    intervals = pd.read_csv(EXP012_INTERVALS, index_col=0, parse_dates=True)
    result['intervals_n_slots'] = len(intervals)
    result['intervals_max_abs_difference_mw'] = max_abs_difference(intervals[['lower', 'upper']], out)
    result['n_pool_equal'] = bool((out.loc[intervals.index, 'n_pool'] == intervals['n_pool']).all())
    result['backup_slots'] = {name: int(out[f'{name}_backup'].sum()) for name in EXPERTS}
    slot_checks = [*result['experts_max_abs_difference_mw'].values(),
                   result['aggregate_max_abs_difference_mw'],
                   result['intervals_max_abs_difference_mw']]
    result['passed'] = bool(all(result['mae_ok'].values()) and result['n_pool_equal']
                            and all(value <= SLOT_TOLERANCE for value in slot_checks))
    return result


def main() -> None:
    check_settings()
    arguments = sys.argv[1:]
    saved_only = bool(arguments) and arguments[0] == '--saved'
    folder = Path(arguments[1 if saved_only else 0]) if len(arguments) > (1 if saved_only else 0)         else DEFAULT_FOLDER
    data = load()
    if saved_only:
        out = pd.read_csv(folder / 'replay_2024.csv', index_col=0, parse_dates=True)
        result = {'state_folder': 'not replayed again', **evaluate(out, data)}
    else:
        if any(folder.glob('*.pkl')):
            raise RuntimeError(f'{folder} already holds states: use an empty folder')
        with connect_database() as connection:
            temperature = read_temperature(connection, WEATHER_FIRST, LAST)
        burn_in = pd.read_csv(CACHE, index_col=0, parse_dates=True)[list(EXPERTS)]
        state = seed_state(burn_in, data.consommation, 'experiment')

        began = time.perf_counter()
        out, _ = replay(FIRST, LAST, data.consommation, temperature, state,
                        store=StateStore(folder))
        seconds = round(time.perf_counter() - began, 1)
        result = {'seconds': seconds, 'state_folder': str(folder), **evaluate(out, data)}
        out.to_csv(folder / 'replay_2024.csv', index_label='value_time_utc')
    result['settings'] = asdict(FROZEN)
    (folder / 'acceptance.json').write_text(json.dumps(result, indent=2, default=str),
                                            encoding='utf-8')
    print(json.dumps(result, indent=2, default=str))
    if not result['passed']:
        raise SystemExit('Acceptance test failed')


if __name__ == '__main__':
    main()
