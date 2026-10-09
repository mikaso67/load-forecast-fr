import time
from datetime import date

import numpy as np
import pandas as pd
import pytest

from load_forecast.backtest import walk_forward
from load_forecast.gam_model import SlotGAM


def synthetic(seed: int = 0, n: int = 800):
    rng = np.random.default_rng(seed)
    temperature = rng.uniform(-5, 30, n)
    weekend = rng.integers(0, 2, n).astype(float)
    truth = 3000 + 10 * (temperature - 15) ** 2 + 500 * weekend
    x = pd.DataFrame({'temperature_c': temperature, 'weekend': weekend})
    return x, pd.Series(truth + rng.normal(0, 50, n)), truth


def test_recovers_known_smooth_curve_and_reports_refit_time(capsys):
    x, y, truth = synthetic()
    start = time.perf_counter()
    model = SlotGAM(['temperature_c']).fit(x, y)
    elapsed = time.perf_counter() - start
    error = np.abs(model.predict(x) - truth)
    assert error.mean() < 40
    print(f'GAM refit on {len(x)} rows: {elapsed:.2f} s')
    assert 'GAM refit' in capsys.readouterr().out


def test_deterministic_with_fixed_seed():
    x, y, _ = synthetic(seed=3)
    first = SlotGAM(['temperature_c']).fit(x, y).predict(x)
    second = SlotGAM(['temperature_c']).fit(x, y).predict(x)
    np.testing.assert_array_equal(first, second)


def test_refuses_nan_features_in_fit_and_predict():
    x, y, _ = synthetic()
    broken = x.copy()
    broken.iloc[0, 0] = np.nan
    with pytest.raises(ValueError, match='NaN'):
        SlotGAM(['temperature_c']).fit(broken, y)
    model = SlotGAM(['temperature_c']).fit(x, y)
    with pytest.raises(ValueError, match='NaN'):
        model.predict(broken)


def test_cold_tail_extrapolates_linearly_and_is_continuous():
    rng = np.random.default_rng(1)
    temperature = rng.uniform(0, 30, 800)
    truth = 3000 + 10 * (15 - temperature).clip(0) ** 2
    x = pd.DataFrame({'temperature_c': temperature})
    model = SlotGAM(['temperature_c']).fit(x, pd.Series(truth + rng.normal(0, 30, 800)))
    cold = model.lower_['temperature_c']
    grid = pd.DataFrame({'temperature_c': [cold + 1e-6, cold, cold - 5.0, cold - 10.0, cold - 15.0]})
    pred = model.predict(grid)
    assert abs(pred[0] - pred[1]) < 1.0
    assert pred[2] > pred[1] and pred[3] > pred[2] and pred[4] > pred[3]
    np.testing.assert_allclose(pred[3] - pred[2], pred[4] - pred[3], rtol=1e-6)
    assert model.slope_lower_['temperature_c'] < 0


def test_hot_tail_extrapolates_with_upper_boundary_slope():
    rng = np.random.default_rng(2)
    temperature = rng.uniform(0, 30, 800)
    truth = 3000 + 10 * (temperature - 15).clip(0) ** 2
    x = pd.DataFrame({'temperature_c': temperature})
    model = SlotGAM(['temperature_c']).fit(x, pd.Series(truth + rng.normal(0, 30, 800)))
    hot = model.upper_['temperature_c']
    pred = model.predict(pd.DataFrame({'temperature_c': [hot, hot + 5.0, hot + 10.0]}))
    assert pred[1] > pred[0] and pred[2] > pred[1]
    np.testing.assert_allclose(pred[2] - pred[1], pred[1] - pred[0], rtol=1e-6)


def test_record_cold_day_outside_training_range_raises_forecast():
    rng = np.random.default_rng(4)
    temperature = rng.uniform(-2, 30, 800)
    truth = 3000 + 10 * (15 - temperature).clip(0) ** 2
    x = pd.DataFrame({'temperature_c': temperature, 'weekend': 0.0})
    model = SlotGAM(['temperature_c']).fit(x, pd.Series(truth + rng.normal(0, 30, 800)))
    cold = pd.DataFrame({'temperature_c': [-4.0, -9.0, -14.0], 'weekend': 0.0})
    pred = model.predict(cold)
    assert np.all(np.diff(pred) > 0)
    assert pred[0] > model.predict(pd.DataFrame({'temperature_c': [-2.0], 'weekend': [0.0]}))[0]


def test_constant_column_and_out_of_range_prediction_are_safe():
    x, y, _ = synthetic()
    x['christmas_week'] = 0.0
    model = SlotGAM(['temperature_c']).fit(x, y)
    outside = pd.DataFrame({'temperature_c': [60.0], 'weekend': [0.0], 'christmas_week': [1.0]})
    assert np.isfinite(model.predict(outside)).all()


def test_runs_in_walk_forward_on_a_50_slot_day():
    index = pd.date_range('2021-03-22 23:00', '2021-11-02 22:30', freq='30min', tz='UTC')
    history = pd.Series(40000 + 100 * np.arange(len(index)) % 7000.0, index=index)

    def builder(slots, _history, day):
        return pd.DataFrame({'temperature_c': float(day.timetuple().tm_yday % 30),
                             'weekend': float(day.weekday() >= 5)}, index=slots)

    first, last = date(2021, 10, 30), date(2021, 11, 1)
    predictions = walk_forward(history, first, last, lambda: SlotGAM(['temperature_c']),
                               builder=builder)
    assert len(predictions) == 48 + 50 + 48
    assert predictions['model'].notna().all()


def test_runs_in_walk_forward_on_a_46_slot_spring_day():
    index = pd.date_range('2021-03-22 23:00', '2022-04-02 22:30', freq='30min', tz='UTC')
    history = pd.Series(40000 + 100 * np.arange(len(index)) % 7000.0, index=index)

    def builder(slots, _history, day):
        return pd.DataFrame({'temperature_c': float(day.timetuple().tm_yday % 30),
                             'weekend': float(day.weekday() >= 5)}, index=slots)

    first, last = date(2022, 3, 27), date(2022, 3, 27)
    predictions = walk_forward(history, first, last, lambda: SlotGAM(['temperature_c']),
                               builder=builder)
    expected = pd.date_range('2022-03-26 23:00', '2022-03-27 21:30', freq='30min', tz='UTC')
    assert len(predictions) == 46
    assert predictions.index.equals(expected)
    assert predictions['model'].notna().all() and np.isfinite(predictions['model']).all()
