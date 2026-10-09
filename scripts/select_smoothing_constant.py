"""EXP-006 smoothing constant selection (D13): half life of T_s chosen on 2023 only.

Inner walk-forward over 2023 (weekly refits from 2021-03-23, same runner, GAM penalty 100 and EXP-005
features plus the smoothed effect), one run per candidate half life. Consumption after 2023-12-31 is
deleted before the runner starts and weather is read to 2023-12-31 only, so nothing from 2024 can
reach this choice. The chosen half life is then frozen in run_exp006.py.
"""

import json
import sys
import time
from datetime import date, timedelta

import pandas as pd
from run_exp002 import TRAINING_START, load
from run_exp006 import make_gam2

from load_forecast import evaluation as ev
from load_forecast.backtest import walk_forward
from load_forecast.eco2mix import connect_database
from load_forecast.smoothed_temperature import smoothed_gam_features, smoothing_constant
from load_forecast.weather_features import read_temperature

INNER_FIRST, INNER_LAST = date(2023, 1, 1), date(2023, 12, 31)
WEATHER_FIRST = date(2021, 3, 24)
CANDIDATE_HALF_LIVES_HOURS = (6.0, 12.0, 24.0, 48.0, 96.0, 192.0, 384.0)


def main() -> None:
    data = load()
    end = pd.Timestamp(INNER_LAST + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
    history = data.consommation.loc[data.index < end]
    with connect_database() as connection:
        temperature = read_temperature(connection, WEATHER_FIRST, INNER_LAST)
    candidates = [float(h) for h in sys.argv[1:]] or list(CANDIDATE_HALF_LIVES_HOURS)
    rows = {}
    for hours in candidates:
        a = smoothing_constant(hours)
        start = time.perf_counter()
        predictions = walk_forward(history, INNER_FIRST, INNER_LAST, make_gam2,
                                   builder=smoothed_gam_features(temperature, a),
                                   training_start=TRAINING_START)
        kept = predictions[[ev.MODEL]].assign(**{ev.ACTUAL: history}).dropna()
        error = (kept[ev.MODEL] - kept[ev.ACTUAL]).abs()
        rows[hours] = {'a': a, 'mae_2023': float(error.mean()), 'n_slots': int(len(kept)),
                       'seconds': round(time.perf_counter() - start, 1)}
        print(hours, rows[hours], flush=True)
    print(json.dumps({'inner_period': '2023', 'gam_penalty': 100.0, 'rows': rows}))


if __name__ == '__main__':
    main()
