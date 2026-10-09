"""EXP-008 hyperparameter choice on 2023 only (D13, M10 A).

Inner walk-forward over 2023 (weekly refits from 2021-03-23, same features and runner as the 2024
backtest). One fit per week and per (learning rate, leaves, min data) triple; the candidate round
counts are scored from prefixes of the same trees. Consumption after 2023-12-31 is deleted before
the runner starts and weather is read to 2023-12-31 only, so nothing from 2024 can reach this choice.
Grid and tie rule are fixed in EXP-008.md before this script is run.

Second stage (documented in EXP-008.md): `extend LOG lr,leaves,min_data ...` re-runs the named
triples with more rounds (3200, 6400) when the grid winner sits on the 1600-round edge, then applies
the same tie rule to the union of both stages. Still 2023 only.
"""

import ast
import itertools
import json
import sys
import time
from datetime import date, timedelta

import pandas as pd
from run_exp002 import TRAINING_START, load

from load_forecast import evaluation as ev
from load_forecast.eco2mix import connect_database
from load_forecast.gam_model import lagged_gam_features
from load_forecast.gbm_model import GBMConfig, walk_forward_global
from load_forecast.weather_features import read_temperature

INNER_FIRST, INNER_LAST = date(2023, 1, 1), date(2023, 12, 31)
WEATHER_FIRST = date(2021, 3, 24)
LEARNING_RATES = (0.03, 0.1)
NUM_LEAVES = (15, 63)
MIN_DATA = (20, 100)
ROUNDS = (100, 200, 400, 800, 1600)
EXTENDED_ROUNDS = (3200, 6400)
TIE_MW = 1.0


def score(predictions: pd.DataFrame, history: pd.Series) -> dict:
    kept = predictions[[ev.MODEL]].assign(**{ev.ACTUAL: history}).dropna()
    return {'mae_2023': float((kept[ev.MODEL] - kept[ev.ACTUAL]).abs().mean()),
            'n_slots': int(len(kept))}


def choose(rows: list[dict]) -> dict:
    """Lowest 2023 MAE; within TIE_MW of it, fewest leaves, then fewest rounds, then larger
    min_data_in_leaf, then larger learning rate."""
    best = min(row['mae_2023'] for row in rows)
    tied = [row for row in rows if row['mae_2023'] <= best + TIE_MW]
    return min(tied, key=lambda r: (r['num_leaves'], r['rounds'], -r['min_data_in_leaf'],
                                    -r['learning_rate']))


def run_triples(triples, rounds: tuple[int, ...]) -> list[dict]:
    data = load()
    end = pd.Timestamp(INNER_LAST + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
    history = data.consommation.loc[data.index < end]
    with connect_database() as connection:
        temperature = read_temperature(connection, WEATHER_FIRST, INNER_LAST)
    builder = lagged_gam_features(temperature)
    rows = []
    for lr, leaves, min_data in triples:
        began = time.perf_counter()
        config = GBMConfig(lr, leaves, min_data, max(rounds))
        forecasts = walk_forward_global(history, INNER_FIRST, INNER_LAST, config, builder=builder,
                                        training_start=TRAINING_START, views=rounds)
        seconds = round(time.perf_counter() - began, 1)
        for n_rounds, frame in forecasts.items():
            rows.append({'learning_rate': lr, 'num_leaves': leaves, 'min_data_in_leaf': min_data,
                         'rounds': n_rounds, **score(frame, history),
                         'seconds_for_the_set': seconds})
            print(rows[-1], flush=True)
    return rows


def report(rows: list[dict]) -> None:
    if len({row['n_slots'] for row in rows}) != 1:
        raise RuntimeError('Candidates score different slots')
    print(json.dumps({'inner_period': '2023', 'chosen': choose(rows), 'rows': rows}, indent=2))


def main() -> None:
    if sys.argv[1:2] == ['extend']:
        log, *named = sys.argv[2:]
        with open(log, encoding='utf-8-sig') as handle:
            first_stage = [ast.literal_eval(line) for line in handle if line.startswith("{'learning")]
        triples = [tuple(f(v) for f, v in zip((float, int, int), item.split(','), strict=True))
                   for item in named]
        report(first_stage + run_triples(triples, EXTENDED_ROUNDS))
        return
    report(run_triples(list(itertools.product(LEARNING_RATES, NUM_LEAVES, MIN_DATA)), ROUNDS))


if __name__ == '__main__':
    main()
