"""EXP-005 smoothing selection (D13, M10 A): one penalty weight for the 48 slot GAMs, chosen on 2023.

Inner walk-forward over 2023 only (weekly refits from 2021-03-23, same runner and features as the
2024 backtest), one run per candidate weight. Consumption after 2023-12-31 is deleted before the runner
starts, so nothing from 2024 can reach this choice. The chosen weight is then frozen in run_exp005.py.
"""

import json
import sys
import time
from datetime import date, timedelta

import pandas as pd
from run_exp002 import TRAINING_START, load
from run_exp005 import DEGREES_OF_FREEDOM, make_gam

from load_forecast import evaluation as ev
from load_forecast.backtest import walk_forward
from load_forecast.eco2mix import connect_database
from load_forecast.gam_model import lagged_gam_features
from load_forecast.weather_features import read_temperature

INNER_FIRST, INNER_LAST = date(2023, 1, 1), date(2023, 12, 31)
WEATHER_FIRST = date(2021, 3, 24)
CANDIDATES = (1e-1, 1e0, 1e1, 1e2, 1e3, 1e4, 1e5, 1e6)


def main() -> None:
    data = load()
    end = pd.Timestamp(INNER_LAST + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
    history = data.consommation.loc[data.index < end]
    with connect_database() as connection:
        temperature = read_temperature(connection, WEATHER_FIRST, INNER_LAST)
    builder = lagged_gam_features(temperature)
    candidates = [float(a) for a in sys.argv[1:]] or list(CANDIDATES)
    rows = {}
    for alpha in candidates:
        start = time.perf_counter()
        predictions = walk_forward(history, INNER_FIRST, INNER_LAST, lambda a=alpha: make_gam(a),
                                   builder=builder, training_start=TRAINING_START)
        frame = predictions[[ev.MODEL]].assign(**{ev.ACTUAL: history})
        kept = frame.dropna()
        error = (kept[ev.MODEL] - kept[ev.ACTUAL]).abs()
        rows[alpha] = {'mae_2023': float(error.mean()), 'n_slots': int(len(kept)),
                       'seconds': round(time.perf_counter() - start, 1)}
        print(alpha, rows[alpha], flush=True)
    print(json.dumps({'degrees_of_freedom': DEGREES_OF_FREEDOM, 'inner_period': '2023', 'rows': rows}))


if __name__ == '__main__':
    main()
