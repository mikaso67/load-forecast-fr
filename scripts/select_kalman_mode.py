"""EXP-007 choice on 2023 only (D13): restart the filter after each GAM refit, or keep its state.

Inner walk-forward over 2023 with the EXP-005 GAM (weekly refits from 2021-03-23, penalty 100). Both
modes share the same GAM fits and the same quarterly maximum-likelihood variances. Consumption after
2023-12-31 is deleted before the runner starts and weather is read to 2023-12-31 only, so nothing from
2024 can reach this choice. Rule fixed in EXP-007.md: lowest 2023 MAE, `restart` if within 0.5 MW.
"""

import json
import time
from datetime import date, timedelta

import pandas as pd
from run_exp002 import TRAINING_START, load
from run_exp006 import make_gam1

from load_forecast import evaluation as ev
from load_forecast.eco2mix import connect_database
from load_forecast.gam_model import lagged_gam_features
from load_forecast.kalman import walk_forward_kalman
from load_forecast.weather_features import read_temperature

INNER_FIRST, INNER_LAST = date(2023, 1, 1), date(2023, 12, 31)
WEATHER_FIRST = date(2021, 3, 24)
TIE_MW = 0.5


def mae(predictions: pd.DataFrame, history: pd.Series) -> dict:
    kept = predictions[[ev.MODEL]].assign(**{ev.ACTUAL: history}).dropna()
    return {'mae_2023': float((kept[ev.MODEL] - kept[ev.ACTUAL]).abs().mean()),
            'n_slots': int(len(kept))}


def main() -> None:
    data = load()
    end = pd.Timestamp(INNER_LAST + timedelta(days=1), tz='Europe/Paris').tz_convert('UTC')
    history = data.consommation.loc[data.index < end]
    with connect_database() as connection:
        temperature = read_temperature(connection, WEATHER_FIRST, INNER_LAST)
    began = time.perf_counter()
    result = walk_forward_kalman(history, INNER_FIRST, INNER_LAST, make_gam1,
                                 builder=lagged_gam_features(temperature),
                                 training_start=TRAINING_START)
    rows = {'gam': mae(result.gam, history),
            **{mode: mae(frame, history) for mode, frame in result.corrected.items()}}
    # Same slots for the three: a Kalman NaN is a GAM NaN, so n_slots must agree.
    if len({row['n_slots'] for row in rows.values()}) != 1:
        raise RuntimeError('Modes score different slots')
    gap = rows['keep']['mae_2023'] - rows['restart']['mae_2023']
    choice = 'restart' if gap > -TIE_MW else 'keep'
    print(json.dumps({'inner_period': '2023', 'rows': rows, 'keep_minus_restart_mw': gap,
                      'choice': choice, 'seconds': round(time.perf_counter() - began, 1),
                      'variance_fits': [{**f, 'refit': str(f['refit'])} for f in result.fits]},
                     indent=2))


if __name__ == '__main__':
    main()
