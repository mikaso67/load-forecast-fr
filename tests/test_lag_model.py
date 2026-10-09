from datetime import date

import numpy as np
import pandas as pd
import pytest

from load_forecast import evaluation as ev
from load_forecast.backtest import day_features, walk_forward
from load_forecast.cutoff import delivery_slots
from load_forecast.lag_model import (
    BACKUP_FLAG,
    LAG_COLUMNS,
    SPECIAL_COLUMNS,
    lag_features,
    lagged_ols_features,
    same_local_slot_lag,
)
from load_forecast.linear_model import SlotOLS
from load_forecast.temperature_model import with_calendar_backup


def slots_of(day: date) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(delivery_slots(day, '30min'))


def history_between(first: str, last: str) -> pd.Series:
    """Value = hours elapsed since 2024-01-01 UTC, so every value identifies its own timestamp."""
    index = pd.date_range(first, last, freq='30min', tz='UTC')
    hours = (index - pd.Timestamp('2024-01-01', tz='UTC')) / pd.Timedelta(hours=1)
    return pd.Series(np.asarray(hours, dtype=float), index=index)


def value_at(timestamp: str) -> float:
    return (pd.Timestamp(timestamp, tz='UTC') - pd.Timestamp('2024-01-01', tz='UTC')
            ) / pd.Timedelta(hours=1)


def temperature_frame(first: str, last: str) -> pd.DataFrame:
    index = pd.date_range(first, last, freq='30min', tz='UTC')
    celsius = 8. + 6. * np.sin(np.arange(len(index)) / 97.)
    return pd.DataFrame({'temperature_c': celsius}, index=index)


FULL = history_between('2024-02-20', '2024-11-10 23:30')


def test_ordinary_day_lags_are_the_same_local_slot_two_and_seven_days_earlier():
    day = date(2024, 5, 21)
    slots = slots_of(day)
    features = lag_features(slots, FULL, day)
    assert list(features.columns) == LAG_COLUMNS + SPECIAL_COLUMNS
    np.testing.assert_allclose(features['lag_d2'], FULL.reindex(slots - pd.Timedelta(days=2)))
    np.testing.assert_allclose(features['lag_d7'], FULL.reindex(slots - pd.Timedelta(days=7)))


@pytest.mark.parametrize('day,expected', [(date(2024, 1, 15), '2024-01-14 08:30'),
                                          (date(2024, 7, 15), '2024-07-14 07:30')])
def test_last_usable_value_is_the_0930_local_slot_of_the_previous_day(day, expected):
    features = lag_features(slots_of(day), history_between('2024-01-01', '2024-07-20'), day)
    assert (features['lag_last'] == value_at(expected)).all()


def test_lags_cross_the_spring_change_through_utc_not_row_offsets():
    day = date(2024, 4, 1)  # D-2 is 2024-03-30, CET; D is CEST
    slots = slots_of(day)
    features = lag_features(slots, FULL, day)
    assert features['lag_d2'].iloc[0] == value_at('2024-03-29 23:00')
    assert features['lag_d7'].iloc[0] == value_at('2024-03-24 23:00')
    assert features['lag_d2'].iloc[0] != FULL.iloc[FULL.index.get_loc(slots[0]) - 96]
    assert features['lag_d2'].iloc[-1] == value_at('2024-03-30 22:30')


def test_a_lag_day_that_skipped_the_hour_leaves_the_whole_day_without_lags():
    """D-2 is the 23-hour day: local 02:00 and 02:30 do not exist, so the day falls back."""
    day = date(2024, 4, 2)
    slots = slots_of(day)
    wall = same_local_slot_lag(slots, FULL, 2)
    assert wall.isna().sum() == 2
    features = lag_features(slots, FULL, day)
    assert features.isna().all().all()


def test_lags_cross_the_autumn_change():
    day = date(2024, 10, 29)  # D-2 is the 25-hour day, its 02:xx hour is repeated
    slots = slots_of(day)
    lag = same_local_slot_lag(slots, FULL, 2)
    local = slots.tz_convert('Europe/Paris')
    at_0200 = lag[(local.hour == 2) & (local.minute == 0)].iloc[0]
    assert at_0200 == value_at('2024-10-27 01:00')  # second occurrence, kept by D12
    assert lag[(local.hour == 3) & (local.minute == 0)].iloc[0] == value_at('2024-10-27 02:00')
    assert lag.notna().all()


def test_the_25_hour_day_reads_one_lag_value_for_both_repeated_slots():
    day = date(2024, 10, 27)
    slots = slots_of(day)
    assert len(slots) == 50
    features = lag_features(slots, FULL, day)
    local = slots.tz_convert('Europe/Paris')
    repeated = features.loc[(local.hour == 2) & (local.minute == 0), 'lag_d2']
    assert len(repeated) == 2
    assert repeated.nunique() == 1
    assert repeated.iloc[0] == value_at('2024-10-25 00:00')
    assert features.notna().all().all()


def test_special_day_indicators_follow_holiday_and_bridge_of_each_lag_day():
    holiday = lag_features(slots_of(date(2024, 5, 3)), FULL, date(2024, 5, 3))  # D-2 = 1 May
    assert holiday[SPECIAL_COLUMNS].iloc[0].tolist() == [1., 0., 0.]
    last = lag_features(slots_of(date(2024, 5, 2)), FULL, date(2024, 5, 2))  # D-1 = 1 May
    assert last[SPECIAL_COLUMNS].iloc[0].tolist() == [0., 0., 1.]
    bridge = lag_features(slots_of(date(2024, 5, 12)), FULL, date(2024, 5, 12))  # D-2 = bridge
    assert ev.is_bridge(date(2024, 5, 10))
    assert bridge['special_d2'].iloc[0] == 1.
    week = lag_features(slots_of(date(2024, 5, 8)), FULL, date(2024, 5, 8))  # D-7 = 1 May
    assert week['special_d7'].iloc[0] == 1.


def test_any_missing_lag_removes_the_features_of_the_whole_day():
    day = date(2024, 5, 21)
    slots = slots_of(day)
    for hole in (slots[17] - pd.Timedelta(days=7), slots[40] - pd.Timedelta(days=2),
                 pd.Timestamp(ev.last_usable_slot_start(day))):
        damaged = FULL.drop(hole)
        features = lag_features(slots, damaged, day)
        assert features.isna().all().all(), hole
        assert len(features) == 48
    assert lag_features(slots, FULL, day).notna().all().all()


def leaking_builder(slots, history, day):
    """Mutant: the same slot of D-1, published after the cutoff for most slots."""
    return pd.DataFrame({'lag_d1': history.reindex(slots - pd.Timedelta(days=1)).to_numpy()},
                        index=slots)


def latest_value_builder(slots, history, day):
    """Mutant: the latest value in whatever history it receives."""
    return pd.DataFrame({'latest': float(history.iloc[-1])}, index=slots)


def assert_unchanged_without_late_values(builder, history, day):
    """Hand the builder the full history, then the history with all post-D2 values deleted."""
    slots = slots_of(day)
    early = history.loc[history.index <= ev.last_usable_slot_start(day)]
    assert len(early) < len(history)
    pd.testing.assert_frame_equal(builder(slots, history, day), builder(slots, early, day))


@pytest.mark.parametrize('day', [date(2024, 3, 12), date(2024, 5, 21), date(2024, 10, 28)])
def test_lag_features_do_not_change_when_late_consumption_is_deleted(day):
    assert_unchanged_without_late_values(lag_features_builder, FULL, day)


def lag_features_builder(slots, history, day):
    return lag_features(slots, history, day)


@pytest.mark.parametrize('mutant', [leaking_builder, latest_value_builder])
def test_the_truncation_check_fails_on_a_deliberately_leaking_builder(mutant):
    with pytest.raises(AssertionError):
        assert_unchanged_without_late_values(mutant, FULL, date(2024, 5, 21))


def test_lagged_builder_extends_the_weather_builder_on_the_exact_grid():
    temperature = temperature_frame('2024-02-29 23:00', '2024-03-26 23:30')
    history = history_between('2024-02-20', '2024-03-26')
    day = date(2024, 3, 20)
    features = day_features(history, day, lagged_ols_features(temperature))
    assert len(features) == 48
    assert {'hdd', 'cdd', 'dow_0', 'bridge'} <= set(features.columns)
    assert list(features.columns[-6:]) == LAG_COLUMNS + SPECIAL_COLUMNS
    assert features.notna().all().all()


def test_walk_forward_truncation_with_lags():
    """Predictions for D do not change when every later consumption value is removed."""
    temperature = temperature_frame('2024-02-29 23:00', '2024-03-26 23:30')
    rng = np.random.default_rng(5)
    history = history_between('2024-02-20', '2024-03-26')
    history = history + rng.normal(0, 3, len(history))
    builder = lagged_ols_features(temperature)
    for day in (date(2024, 3, 13), date(2024, 3, 14), date(2024, 3, 22)):
        boundary = ev.last_usable_slot_start(day)
        kwargs = {'training_start': date(2024, 2, 21), 'builder': builder}
        full = walk_forward(history, day, day, SlotOLS, **kwargs)
        short = walk_forward(history.loc[history.index <= boundary], day, day, SlotOLS, **kwargs)
        pd.testing.assert_frame_equal(full, short)
        assert full[ev.MODEL].notna().all()


def test_day_with_a_missing_lag_is_excluded_from_the_fit_and_forecast_by_the_backup():
    temperature = temperature_frame('2024-02-29 23:00', '2024-03-26 23:30')
    history = history_between('2024-02-20', '2024-03-26')
    day = date(2024, 3, 20)
    hole = slots_of(day)[10] - pd.Timedelta(days=7)
    damaged = history.drop(hole)
    builder = lagged_ols_features(temperature)
    kwargs = {'training_start': date(2024, 2, 21), 'builder': builder}
    lagged = walk_forward(damaged, day, day, SlotOLS, **kwargs)
    assert lagged[ev.MODEL].isna().all()
    backup = lagged.assign(**{ev.MODEL: 7.})
    combined = with_calendar_backup(lagged, backup, flag=BACKUP_FLAG)
    assert combined[BACKUP_FLAG].all()
    assert (combined[ev.MODEL] == 7.).all()
    healthy = walk_forward(history, day, day, SlotOLS, **kwargs)
    assert healthy[ev.MODEL].notna().all()


def test_integer_history_and_a_missing_lag_do_not_break_the_nan_rule():
    """The database gives integer MW when nothing is missing; the missing-lag rule must still work."""
    day = date(2024, 5, 21)
    integer = (FULL * 100).round().astype('int64')
    features = lag_features(slots_of(day), integer, day)
    assert features.dtypes.eq(float).all()
    assert features.notna().all().all()
    hole = integer.drop(slots_of(day)[3] - pd.Timedelta(days=7))
    assert lag_features(slots_of(day), hole, day).isna().all().all()
