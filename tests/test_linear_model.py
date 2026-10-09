from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from load_forecast import evaluation as ev
from load_forecast.backtest import day_features, local_slots, walk_forward
from load_forecast.cutoff import forecast_cutoff
from load_forecast.linear_model import DAY_COLUMNS, SlotOLS, calendar_ols_features


def features_for(day):
    return day_features(pd.Series(dtype=float, index=pd.DatetimeIndex([], tz='UTC')), day,
                        calendar_ols_features).iloc[0]


def test_holiday_is_coded_as_sunday():
    wednesday_holiday = features_for(date(2024, 5, 1))
    sunday = features_for(date(2024, 5, 5))
    assert wednesday_holiday[DAY_COLUMNS].sum() == 0
    pd.testing.assert_series_equal(wednesday_holiday[DAY_COLUMNS], sunday[DAY_COLUMNS],
                                   check_names=False)


def test_weekday_dummy_and_bridge_and_christmas():
    assert features_for(date(2024, 4, 9))['dow_1'] == 1
    assert features_for(date(2024, 5, 10))['bridge'] == 1
    assert features_for(date(2024, 5, 3))['bridge'] == 0
    assert features_for(date(2024, 12, 24))['christmas_week'] == 1
    assert features_for(date(2024, 12, 31))['christmas_week'] == 1
    assert features_for(date(2024, 12, 23))['christmas_week'] == 0
    assert features_for(date(2024, 1, 2))['christmas_week'] == 0


@pytest.mark.parametrize('day,n', [(date(2024, 3, 31), 46), (date(2024, 10, 27), 50)])
def test_dst_days_keep_the_exact_grid(day, n):
    history = pd.Series(dtype=float, index=pd.DatetimeIndex([], tz='UTC'))
    features = day_features(history, day, calendar_ols_features)
    assert len(features) == n
    assert features.nunique().max() == 1


def test_builder_ignores_history():
    index = pd.date_range('2024-03-01', '2024-04-09', freq='30min', tz='UTC', inclusive='left')
    history = pd.Series(np.arange(len(index), dtype=float), index=index)
    day = date(2024, 4, 7)
    published = pd.Series(history.index + pd.Timedelta(hours=3), index=history.index)
    truncated = history.loc[published < forecast_cutoff(day)]
    pd.testing.assert_frame_equal(day_features(history, day, calendar_ols_features, published),
                                  day_features(truncated, day, calendar_ols_features, published))


def test_slot_ols_recovers_exact_coefficients_and_tolerates_constant_column():
    rng = np.random.default_rng(0)
    x = pd.DataFrame({'a': rng.normal(size=50), 'b': rng.normal(size=50), 'const': 0.0})
    y = 100 + 10 * x['a'] - 5 * x['b']
    model = SlotOLS().fit(x, y)
    np.testing.assert_allclose(model.predict(x), y, atol=1e-8)


def synthetic_load():
    index = pd.date_range('2023-10-01', '2024-04-15', freq='30min', tz='UTC', inclusive='left')
    local = index.tz_convert('Europe/Paris')
    days = pd.Series(local.date, index=index)
    base = 50000 + 1000 * (local.hour * 2 + local.minute // 30)
    effective = pd.Series([6 if ev.is_holiday(d) else d.weekday() for d in days], index=index)
    return pd.Series(base - 5000 * (effective == 6).to_numpy(), index=index, dtype=float)


def test_walk_forward_learns_the_weekday_effect_and_the_holiday_as_sunday():
    history = synthetic_load()
    day = date(2024, 4, 1)  # Easter Monday: a holiday on a Monday
    predictions = walk_forward(history, day, day, SlotOLS, builder=calendar_ols_features,
                               training_start=date(2023, 10, 1))
    target = history.reindex(predictions.index)
    sunday_level = history.loc[ev.local_dates(history.index) == date(2024, 3, 31)]
    assert ev.is_holiday(day)
    np.testing.assert_allclose(predictions[ev.MODEL], target, atol=1e-6)
    assert sunday_level.mean() < history.loc[ev.local_dates(history.index) == date(2024, 3, 25)].mean()


@pytest.mark.parametrize('day,n', [(date(2024, 3, 31), 46), (date(2024, 10, 27), 50)])
def test_walk_forward_on_dst_days_uses_one_model_per_local_slot(day, n):
    index = pd.date_range(day - timedelta(days=90), day + timedelta(days=1), freq='30min',
                          tz='UTC', inclusive='left')
    local = index.tz_convert('Europe/Paris')
    history = pd.Series(1000.0 * (local.hour * 2 + local.minute // 30), index=index)
    predictions = walk_forward(history, day, day, SlotOLS, builder=calendar_ols_features,
                               training_start=day - timedelta(days=90))
    assert len(predictions) == n
    np.testing.assert_allclose(predictions[ev.MODEL], history.reindex(predictions.index),
                               atol=1e-6)
    if n == 50:
        assert (local_slots(predictions.index) == 4).sum() == 2
