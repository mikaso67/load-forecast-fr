from datetime import date

import numpy as np
import pandas as pd
import pytest

from load_forecast import evaluation as ev
from load_forecast.backtest import local_slots, walk_forward
from load_forecast.cutoff import delivery_slots
from load_forecast.gam_model import lagged_gam_features
from load_forecast.gbm_model import (
    SLOT_COLUMN,
    GBMConfig,
    GlobalGBM,
    booster_params,
    walk_forward_global,
)

CONFIG = GBMConfig(learning_rate=0.1, num_leaves=7, min_data_in_leaf=5, rounds=12)
START = date(2023, 11, 1)


def slots_of(day: date) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(delivery_slots(day, '30min'))


def setup(seed: int = 5):
    """Hours since 2024-01-01 plus noise as consumption, a slow sine as the temperature."""
    index = pd.date_range('2023-10-20', '2024-03-26 23:30', freq='30min', tz='UTC')
    hours = np.asarray((index - pd.Timestamp('2024-01-01', tz='UTC')) / pd.Timedelta(hours=1))
    history = pd.Series(hours + np.random.default_rng(seed).normal(0, 3, len(index)), index=index)
    weather = pd.date_range('2023-10-31 23:00', '2024-03-26 23:30', freq='30min', tz='UTC')
    temperature = pd.DataFrame({'temperature_c': 8. + 6. * np.sin(np.arange(len(weather)) / 97.)},
                               index=weather)
    return temperature, history


def toy_rows(n_days: int = 40, seed: int = 0):
    """Two days-worth structure: the target depends on the slot and on the temperature."""
    rng = np.random.default_rng(seed)
    index = pd.date_range('2024-01-01', periods=n_days * 48, freq='30min', tz='UTC')
    local = index.tz_convert('Europe/Paris')
    x = pd.DataFrame({'temperature_c': rng.uniform(-5, 20, len(index))}, index=index)
    y = pd.Series(40000 + 800 * (local.hour * 2 + local.minute // 30) - 300 * x.temperature_c
                  + rng.normal(0, 50, len(index)), index=index)
    return x, y


def test_the_slot_is_read_from_the_timestamps_and_added_as_the_last_column():
    x, y = toy_rows()
    model = GlobalGBM(CONFIG).fit(x, y)
    assert model.columns_ == ['temperature_c', SLOT_COLUMN]
    day = x.iloc[:48]
    out = pd.Series(model.predict(day), index=local_slots(day.index))
    assert np.isfinite(out).all()
    # Slot 47 sits far above slot 0 in the toy target: the model really uses the slot.
    assert out.loc[47] - out.loc[0] > 5000


def test_autumn_change_day_has_the_second_occurrence_on_the_same_slot_and_spring_day_misses_slots():
    autumn = pd.DatetimeIndex(delivery_slots(date(2024, 10, 27), '30min'))
    spring = pd.DatetimeIndex(delivery_slots(date(2024, 3, 31), '30min'))
    x_autumn = pd.DataFrame({'t': 1.0}, index=autumn)
    x_spring = pd.DataFrame({'t': 1.0}, index=spring)
    slots = GlobalGBM._with_slot(x_autumn)[SLOT_COLUMN]
    assert len(autumn) == 50 and slots.value_counts().max() == 2
    assert sorted(slots.unique()) == list(range(48))
    assert len(spring) == 46 and GlobalGBM._with_slot(x_spring)[SLOT_COLUMN].nunique() == 46


def test_nan_rows_stay_nan_and_complete_rows_are_unchanged():
    x, y = toy_rows()
    model = GlobalGBM(CONFIG).fit(x, y)
    probe = x.iloc[:6].copy()
    probe.iloc[2, 0] = np.nan
    out = model.predict(probe)
    assert np.isnan(out[2]) and np.isfinite(np.delete(out, 2)).all()
    np.testing.assert_array_equal(np.delete(out, 2), model.predict(x.iloc[[0, 1, 3, 4, 5]]))
    assert model.predict(probe.iloc[:0]).shape == (0,)


def test_fit_refuses_missing_values():
    x, y = toy_rows()
    x.iloc[3, 0] = np.nan
    with pytest.raises(ValueError, match='complete'):
        GlobalGBM(CONFIG).fit(x, y)


def test_the_same_data_gives_the_same_forecast():
    x, y = toy_rows()
    first = GlobalGBM(CONFIG).fit(x, y).predict(x)
    second = GlobalGBM(CONFIG).fit(x, y).predict(x)
    np.testing.assert_array_equal(first, second)


def test_a_prefix_of_the_trees_is_the_model_trained_for_that_many_rounds():
    x, y = toy_rows()
    long = GlobalGBM(CONFIG).fit(x, y)
    short = GlobalGBM(GBMConfig(0.1, 7, 5, rounds=5)).fit(x, y)
    np.testing.assert_allclose(long.predict(x, rounds=5), short.predict(x), rtol=0, atol=1e-8)
    assert not np.allclose(long.predict(x, rounds=5), long.predict(x))


def test_the_objective_is_l1_and_nothing_random_is_left_on():
    params = booster_params(CONFIG)
    assert params['objective'] == 'l1'
    assert params['bagging_fraction'] == 1.0 and params['feature_fraction'] == 1.0
    assert params['deterministic'] is True


def forecast(history, temperature, day, **kwargs):
    return walk_forward_global(history, day, day, CONFIG, builder=lagged_gam_features(temperature),
                               training_start=START, **kwargs)


def test_forecast_is_unchanged_when_consumption_after_the_cutoff_is_removed():
    temperature, history = setup()
    for day in (date(2024, 3, 13), date(2024, 3, 14), date(2024, 3, 22)):
        boundary = ev.last_usable_slot_start(day)
        full = forecast(history, temperature, day)[CONFIG.rounds]
        short = forecast(history.loc[history.index <= boundary], temperature, day)[CONFIG.rounds]
        pd.testing.assert_frame_equal(full, short)
        assert full[ev.MODEL].notna().all()


def test_a_perturbation_of_the_last_published_day_changes_nothing_but_the_lags_it_feeds():
    temperature, history = setup()
    day = date(2024, 3, 20)
    base = forecast(history, temperature, day)[CONFIG.rounds][ev.MODEL]
    after = history.copy()
    after.loc[slots_of(day)[5]] += 5000.0  # a slot of D itself: published after the cutoff
    unchanged = forecast(after, temperature, day)[CONFIG.rounds][ev.MODEL]
    pd.testing.assert_series_equal(base, unchanged)


def test_a_day_with_a_missing_lag_or_no_weather_gets_nan():
    temperature, history = setup()
    day = date(2024, 3, 20)
    damaged = history.drop(slots_of(day)[10] - pd.Timedelta(days=7))
    assert forecast(damaged, temperature, day)[CONFIG.rounds][ev.MODEL].isna().all()
    gap = temperature.copy()
    gap.loc[slots_of(day), 'temperature_c'] = float('nan')
    assert forecast(history, gap, day)[CONFIG.rounds][ev.MODEL].isna().all()


def test_refits_follow_the_weekly_monday_schedule_of_the_slot_models():
    temperature, history = setup()
    first, last = date(2024, 3, 13), date(2024, 3, 26)
    ours = walk_forward_global(history, first, last, CONFIG,
                               builder=lagged_gam_features(temperature),
                               training_start=START)[CONFIG.rounds]
    reference = walk_forward(history, first, last, lambda: _Mean(),
                             builder=lagged_gam_features(temperature), training_start=START)
    pd.testing.assert_series_equal(ours['refit_date'], reference['refit_date'])
    pd.testing.assert_series_equal(ours['refit_cutoff'], reference['refit_cutoff'])


class _Mean:
    def fit(self, x, y):
        return self

    def predict(self, x):
        return np.zeros(len(x))


def test_views_give_one_frame_per_round_count_and_are_validated():
    temperature, history = setup()
    day = date(2024, 3, 20)
    out = forecast(history, temperature, day, views=(4, 12))
    assert set(out) == {4, 12}
    assert not np.allclose(out[4][ev.MODEL], out[12][ev.MODEL])
    for bad in ((13,), (0,), ()):
        with pytest.raises(ValueError, match='between 1'):
            forecast(history, temperature, day, views=bad)
    with pytest.raises(ValueError, match='after training_start'):
        walk_forward_global(history, date(2025, 1, 1), date(2025, 1, 2), CONFIG,
                            builder=lagged_gam_features(temperature), training_start=START)
