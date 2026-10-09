from datetime import date

import numpy as np
import pandas as pd
import pytest

from load_forecast import evaluation as ev
from load_forecast.backtest import day_features, walk_forward
from load_forecast.cutoff import delivery_slots
from load_forecast.gam_model import TEMPERATURE_COLUMN, SlotGAM, lagged_gam_features
from load_forecast.lag_model import LAG_COLUMNS, SPECIAL_COLUMNS, lagged_ols_features

FIRST_DAY = '2023-10-31 23:00'
LAST_DAY = '2024-03-26 23:30'
ALPHA = (1e3,)


def slots_of(day: date) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(delivery_slots(day, '30min'))


def history_between(first: str, last: str) -> pd.Series:
    """Value = hours elapsed since 2024-01-01 UTC, so every value identifies its own timestamp."""
    index = pd.date_range(first, last, freq='30min', tz='UTC')
    hours = (index - pd.Timestamp('2024-01-01', tz='UTC')) / pd.Timedelta(hours=1)
    return pd.Series(np.asarray(hours, dtype=float), index=index)


def temperature_frame(first: str, last: str) -> pd.DataFrame:
    index = pd.date_range(first, last, freq='30min', tz='UTC')
    return pd.DataFrame({'temperature_c': 8. + 6. * np.sin(np.arange(len(index)) / 97.)},
                        index=index)


def gam_factory():
    return SlotGAM([TEMPERATURE_COLUMN], alpha_grid=ALPHA, missing_as_nan=True)


def setup(seed: int = 5):
    temperature = temperature_frame(FIRST_DAY, LAST_DAY)
    history = history_between('2023-10-20', '2024-03-26')
    history = history + np.random.default_rng(seed).normal(0, 3, len(history))
    return temperature, history


def test_gam_features_replace_the_degree_pair_by_the_temperature_itself():
    temperature, history = setup()
    day = date(2024, 3, 20)
    gam = day_features(history, day, lagged_gam_features(temperature))
    linear = day_features(history, day, lagged_ols_features(temperature))
    assert 'hdd' not in gam and 'cdd' not in gam
    assert set(gam.columns) == (set(linear.columns) - {'hdd', 'cdd'}) | {TEMPERATURE_COLUMN}
    pd.testing.assert_frame_equal(gam[LAG_COLUMNS + SPECIAL_COLUMNS],
                                  linear[LAG_COLUMNS + SPECIAL_COLUMNS])
    np.testing.assert_array_equal(gam[TEMPERATURE_COLUMN],
                                  temperature['temperature_c'].reindex(slots_of(day)))


def test_days_before_the_first_weather_day_have_nan_temperature():
    temperature, history = setup()
    day = date(2023, 11, 1)
    assert day_features(history, day, lagged_gam_features(temperature)
                        )[TEMPERATURE_COLUMN].notna().all()
    early = temperature.loc[temperature.index >= pd.Timestamp('2023-11-05', tz='UTC')]
    frame = day_features(history, date(2023, 11, 2), lagged_gam_features(early))
    assert frame[TEMPERATURE_COLUMN].isna().all()


def test_walk_forward_gam_does_not_change_when_later_consumption_is_removed():
    temperature, history = setup()
    builder = lagged_gam_features(temperature)
    for day in (date(2024, 3, 13), date(2024, 3, 14), date(2024, 3, 22)):
        boundary = ev.last_usable_slot_start(day)
        kwargs = {'training_start': date(2023, 11, 1), 'builder': builder}
        full = walk_forward(history, day, day, gam_factory, **kwargs)
        short = walk_forward(history.loc[history.index <= boundary], day, day,
                             gam_factory, **kwargs)
        pd.testing.assert_frame_equal(full, short)
        assert full[ev.MODEL].notna().all()


def test_a_day_with_a_missing_lag_gets_nan_instead_of_an_error():
    temperature, history = setup()
    day = date(2024, 3, 20)
    damaged = history.drop(slots_of(day)[10] - pd.Timedelta(days=7))
    kwargs = {'training_start': date(2023, 11, 1), 'builder': lagged_gam_features(temperature)}
    broken = walk_forward(damaged, day, day, gam_factory, **kwargs)
    assert broken[ev.MODEL].isna().all() and len(broken) == 48
    assert walk_forward(history, day, day, gam_factory, **kwargs)[ev.MODEL].notna().all()


def test_a_day_without_weather_gets_nan_instead_of_an_error():
    temperature, history = setup()
    day = date(2024, 3, 20)
    gap = temperature.copy()
    gap.loc[slots_of(day), 'temperature_c'] = float('nan')
    kwargs = {'training_start': date(2023, 11, 1)}
    out = walk_forward(history, day, day, gam_factory, builder=lagged_gam_features(gap), **kwargs)
    assert out[ev.MODEL].isna().all()


def test_missing_rows_are_nan_and_complete_rows_unchanged():
    rng = np.random.default_rng(0)
    x = pd.DataFrame({TEMPERATURE_COLUMN: rng.uniform(-5, 30, 400), 'weekend': rng.integers(0, 2, 400)
                      * 1.0})
    y = pd.Series(3000 + 10 * (x[TEMPERATURE_COLUMN] - 15) ** 2 + rng.normal(0, 20, 400))
    model = SlotGAM([TEMPERATURE_COLUMN], alpha_grid=ALPHA, missing_as_nan=True).fit(x, y)
    probe = x.iloc[:6].copy()
    probe.iloc[2, 1] = np.nan
    probe.iloc[4, 0] = np.nan
    out = model.predict(probe)
    assert np.isnan(out[[2, 4]]).all() and np.isfinite(out[[0, 1, 3, 5]]).all()
    np.testing.assert_array_equal(out[[0, 1, 3, 5]], model.predict(x.iloc[[0, 1, 3, 5]]))
    assert model.predict(probe.iloc[:0]).shape == (0,)


def test_strict_mode_still_refuses_missing_values_and_fit_always_does():
    rng = np.random.default_rng(1)
    x = pd.DataFrame({TEMPERATURE_COLUMN: rng.uniform(0, 30, 300)})
    y = pd.Series(1000 + 5 * x[TEMPERATURE_COLUMN] + rng.normal(0, 5, 300))
    strict = SlotGAM([TEMPERATURE_COLUMN], alpha_grid=ALPHA).fit(x, y)
    with pytest.raises(ValueError, match='NaN'):
        strict.predict(x.iloc[:2].assign(**{TEMPERATURE_COLUMN: [np.nan, 1.0]}))
    with pytest.raises(ValueError, match='NaN'):
        SlotGAM([TEMPERATURE_COLUMN], alpha_grid=ALPHA, missing_as_nan=True).fit(
            x.assign(**{TEMPERATURE_COLUMN: [np.nan] + [1.0] * 299}), y)
