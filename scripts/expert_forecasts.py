"""The four frozen experts of EXP-009, each with the configuration and backups of its own run."""

from datetime import date

import pandas as pd
from run_exp002 import TRAINING_START
from run_exp003 import WEATHER_FIRST
from run_exp005 import BACKUP_FLAG_005
from run_exp006 import make_gam1
from run_exp007 import BACKUP_FLAG_007, FILTER_MODE
from run_exp008 import CONFIG

from load_forecast import evaluation as ev
from load_forecast.backtest import walk_forward
from load_forecast.eco2mix import connect_database
from load_forecast.gam_model import lagged_gam_features
from load_forecast.gbm_model import walk_forward_global
from load_forecast.kalman import walk_forward_kalman
from load_forecast.lag_model import BACKUP_FLAG, lagged_ols_features
from load_forecast.linear_model import SlotOLS, calendar_ols_features
from load_forecast.temperature_model import temperature_ols_features, with_calendar_backup
from load_forecast.weather_features import read_temperature

EXPERTS = ('EXP-004', 'EXP-005', 'EXP-007', 'EXP-008')


def expert_forecasts(history: pd.Series, first: date, last: date, *,
                     final_test: bool = False) -> tuple[pd.DataFrame, dict]:
    """Complete forecasts of the four experts on [first, last], and their backup slot counts.

    `history` must already be cut so that nothing after `last` can be read; weather is read to
    `last` only. `final_test` unlocks 2025 for the final test only.
    """
    with connect_database() as connection:
        temperature = read_temperature(connection, WEATHER_FIRST, last, final_test=final_test)
    calendar = walk_forward(history, first, last, SlotOLS, builder=calendar_ols_features,
                            training_start=TRAINING_START, final_test=final_test)
    weather = walk_forward(history, first, last, SlotOLS,
                           builder=temperature_ols_features(temperature),
                           training_start=TRAINING_START, final_test=final_test)
    exp003 = with_calendar_backup(weather, calendar)
    lagged = walk_forward(history, first, last, SlotOLS, builder=lagged_ols_features(temperature),
                          training_start=TRAINING_START, final_test=final_test)
    exp004 = with_calendar_backup(lagged, exp003, flag=BACKUP_FLAG)
    print('OLS chains done', flush=True)
    kalman = walk_forward_kalman(history, first, last, make_gam1,
                                 builder=lagged_gam_features(temperature),
                                 training_start=TRAINING_START, modes=(FILTER_MODE,),
                                 final_test=final_test)
    exp005 = with_calendar_backup(kalman.gam, exp004, flag=BACKUP_FLAG_005)
    exp007 = with_calendar_backup(kalman.corrected[FILTER_MODE], exp005, flag=BACKUP_FLAG_007)
    print('GAM and Kalman done', flush=True)
    raw = walk_forward_global(history, first, last, CONFIG, builder=lagged_gam_features(temperature),
                              training_start=TRAINING_START,
                              final_test=final_test)[CONFIG.rounds]
    exp008 = with_calendar_backup(raw, exp005, flag=BACKUP_FLAG_005)
    frames = {'EXP-004': exp004, 'EXP-005': exp005, 'EXP-007': exp007, 'EXP-008': exp008}
    table = pd.DataFrame({name: frame[ev.MODEL] for name, frame in frames.items()})
    if table.isna().any().any():
        raise RuntimeError('Expert slots without any model')
    flags = {'EXP-004': BACKUP_FLAG, 'EXP-005': BACKUP_FLAG_005, 'EXP-007': BACKUP_FLAG_007,
             'EXP-008': BACKUP_FLAG_005}
    backups = {name: int(frames[name][flags[name]].sum()) for name in frames}
    return table, backups
